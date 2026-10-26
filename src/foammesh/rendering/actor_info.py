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
from foammesh.support.vtk_threads import (
    vtk_run_in_background, vtk_run_in_thread)
from foammesh.support.mesh import Bounds
from foammesh.support import colour_standard
from foammesh.rendering import quality_scale
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
#:
#: Plan 44 MF15 / U1. The mockup draws the grid at ``viewport.lines`` .35 and
#: 0.8 px, and the colour is a THEME token (light on dark, dark on light) that
#: flips with the theme instead of being picked from the surface colour.
EDGE_OPACITY = 0.35
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


#: The colour array of a field coloured by its size (Plan 43 LY12).
MAGNITUDE_SUFFIX = '|d|'


MESH_LINE_DEFAULT_WIDTH = 0.8


@dataclass(frozen=True)
class MeshLineStyle:
    """How interior mesh edges are drawn, on every actor at once.

    ``color`` is ``'auto'`` -- the theme's ``viewport.lines`` token, light on
    the dark theme and dark on the light one -- or a ``#rrggbb`` the user
    chose.
    """
    opacity: float = EDGE_OPACITY
    color: str = 'auto'
    width: float = MESH_LINE_DEFAULT_WIDTH


MESH_LINE_WIDTH_RANGE = (0.5, 3.0)
#: DP-2675. A patch with more faces than this draws its mesh lines no stronger
#: than the defaults, whatever the saved style says. Each face of such a patch
#: is a few pixels across, so lines at full opacity and 1.5 px are not a grid
#: any more: they are a solid dark fill, drawn in front of the patch colours
#: and the quality colours the volume wears underneath. The user's saved
#: style (a persisted 100 % / 1.5 px) turned every meshed model near-black
#: in the live app and left no colour to see.
DENSE_SKIN_FACES = 5_000
#: Auto colours: what a line reads as against a light and a dark surface.
MESH_LINE_DARK = '#1c2530'
MESH_LINE_LIGHT = '#eef2f7'

_meshLineStyle = MeshLineStyle()
#: MF15 / U1. The ``viewport.lines`` token of the theme in force, once a theme
#: has been applied. ``None`` before that (a bare actor in a test).
_themeLineColor = None
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


def themeMeshLineColor():
    """The theme's ``viewport.lines`` colour, or ``None`` before any theme."""
    return _themeLineColor


def setThemeMeshLineColor(color) -> None:
    """MF15 / U1. Record the theme's grid colour for 'auto' lines.

    Each actor re-reads it on its own ``applyTheme``; nothing is repainted
    here because the theme pass already visits every actor.
    """
    global _themeLineColor
    _themeLineColor = str(color) if color else None


def meshLineColorFor(surface: QColor, style: MeshLineStyle = None) -> str:
    """The colour interior edges take on a surface of colour *surface*."""
    style = style or _meshLineStyle
    if style.color != 'auto':
        return style.color
    if _themeLineColor is not None:
        return _themeLineColor
    lum = (0.2126 * surface.redF() + 0.7152 * surface.greenF()
           + 0.0722 * surface.blueF())
    return MESH_LINE_DARK if lum >= 0.45 else MESH_LINE_LIGHT


def highlightEdgeOpacity(style: MeshLineStyle = None) -> float:
    """A selected part's edges stay a step stronger than everyone else's."""
    style = style or _meshLineStyle
    return min(1.0, max(HIGHLIGHT_EDGE_OPACITY, style.opacity * 1.5))


# -- Plan 48 OP: the route-wide Surface opacity ------------------------------ #
#
# One value per route, shown by the viewport rail's Surface opacity tool. The
# Gmsh route draws its model surfaces about 30 % opaque so the points, curves
# and nodes in and behind them stay readable; the snappy route draws them
# opaque. It COMBINES with the per-part fades by multiplying: a part keeps its
# own opacity (the Display control's, the enclosure fade, the see-through fade
# of the poor cells, the seed-placing fade) in ``Properties.opacity``, and what
# the prop is drawn with is that times the route value. A fade that ends puts
# the part's own opacity back, so it returns to the route's value, never to 1.
SURFACE_OPACITY_DEFAULTS = {'gmsh': 0.3, 'snappy': 1.0}
_surfaceOpacity = dict(SURFACE_OPACITY_DEFAULTS)
#: The route the open case is on, or ``None`` before a window has said (a bare
#: actor in a test, a headless run): then nothing is multiplied.
_surfaceRoute = None


class _SurfaceOpacityNotifier(QObject):
    changed = Signal()


_surfaceNotifier = None


def surfaceOpacityNotifier() -> _SurfaceOpacityNotifier:
    """Says when the route or its Surface opacity changed, whoever changed it."""
    global _surfaceNotifier
    if _surfaceNotifier is None:
        _surfaceNotifier = _SurfaceOpacityNotifier()
    return _surfaceNotifier


#: A surface drawn with no opacity at all is not shown and cannot be picked,
#: and the surfaces are always shown: the floor of the Surface opacity.
SURFACE_OPACITY_MIN = 0.02


def _clampOpacity(value) -> float:
    return min(1.0, max(SURFACE_OPACITY_MIN, float(value)))


def surfaceRoute():
    return _surfaceRoute


def routeSurfaceOpacity(route=None) -> float:
    """The Surface opacity of *route* (the current one by default)."""
    route = _surfaceRoute if route is None else route
    return _surfaceOpacity.get(route, 1.0)


def activeSurfaceOpacity(route=None):
    """The Surface opacity in force for *route*, or ``None`` when the open
    case is not on that route (or on none): the legacy drawing."""
    if _surfaceRoute is None or (route is not None and route != _surfaceRoute):
        return None
    return _surfaceOpacity[_surfaceRoute]


def setSurfaceRoute(route) -> None:
    """The window's engine changed: the route's own value now applies."""
    global _surfaceRoute
    if route is not None and route not in SURFACE_OPACITY_DEFAULTS:
        raise ValueError(f'unknown route {route!r}')
    if route == _surfaceRoute:
        return
    _surfaceRoute = route
    _surfaceOpacityChanged()


def setRouteSurfaceOpacity(value, route=None) -> float:
    """Set the Surface opacity of *route* (the current route by default)."""
    route = _surfaceRoute if route is None else route
    if route not in SURFACE_OPACITY_DEFAULTS:
        raise ValueError(f'unknown route {route!r}')
    _surfaceOpacity[route] = _clampOpacity(value)
    _surfaceOpacityChanged()
    return _surfaceOpacity[route]


def resetSurfaceOpacity() -> None:
    """Back to the defaults and no route (tests, a closed case)."""
    global _surfaceRoute
    _surfaceOpacity.update(SURFACE_OPACITY_DEFAULTS)
    _surfaceRoute = None
    _translucentSources.clear()
    _translucentNames.clear()
    _surfaceOpacityChanged()


def _surfaceOpacityChanged() -> None:
    for actor in list(_liveActors):
        try:
            actor.applyRouteOpacity()
        except RuntimeError:
            pass                                   # a destroyed C++ object
    surfaceOpacityNotifier().changed.emit()
    refreshTranslucency()


#: Things that draw translucent surfaces outside any ActorInfo (the Gmsh
#: entity view). Depth peeling is decided by the Display control from its
#: parts; it asks here as well, so a translucent Gmsh model is peeled and a
#: later content change does not switch peeling off under it.
_translucentSources = weakref.WeakSet()
_translucentNames = set()
_translucencyHooks = []


def setSourceTranslucent(source, on: bool) -> None:
    """*source* draws a translucent surface, or stopped. An object is held
    weakly (a view that is dropped stops counting); a name is held as is."""
    try:
        group, add = _translucentSources, _translucentSources.add
        group.discard(source)
    except TypeError:
        group, add = _translucentNames, _translucentNames.add
        group.discard(source)
    if on:
        add(source)


def externalTranslucent() -> bool:
    return bool(len(_translucentSources) or _translucentNames)


def addTranslucencyHook(method) -> None:
    """Call *method* (held weakly) whenever the question of peeling is
    open again."""
    _translucencyHooks.append(weakref.WeakMethod(method))


def refreshTranslucency(fallback=None) -> bool:
    """Ask every hook to decide peeling again. True when one answered; with
    none, *fallback* (a viewport) is told directly."""
    answered = False
    for ref in list(_translucencyHooks):
        hook = ref()
        if hook is None:
            _translucencyHooks.remove(ref)
            continue
        try:
            hook()
            answered = True
        except RuntimeError:
            pass                                   # a destroyed C++ object
        except Exception:                          # noqa: BLE001
            logging.getLogger(__name__).debug('translucency hook failed', exc_info=True)
    if not answered and fallback is not None:
        setter = getattr(fallback, 'setOrderIndependentTransparency', None)
        if setter is not None:
            setter(externalTranslucent())
    return answered


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

#: MF15. A selected part wears ``accent.default`` with a WHITE outline (the
#: mockup's selection); the other roles keep their own colour on both.
HIGHLIGHT_SILHOUETTE_COLOR = '#ffffff'
ACCENT_ROLES = (None, 'selected')


def _accentOr(tokens, fallback: str) -> str:
    try:
        return tokens.value('accent.default')
    except Exception:                                      # noqa: BLE001
        return fallback

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
    #: Plan 41 GN13. Gmsh's own measures, carried as cell arrays by the
    #: session hand-over. The value is the array name.
    GMSH_SICN = 'gmshSICN'
    GMSH_SIGE = 'gmshSIGE'
    GMSH_GAMMA = 'gmshGamma'
    GMSH_ISOTROPY = 'gmshMinIsotropy'
    GMSH_MIN_JACOBIAN = 'gmshMinDetJac'
    GMSH_VOLUME = 'gmshVolume'

    @classmethod
    def values(cls):
        return [c.value for c in cls]

    @classmethod
    def openfoam(cls):
        """The four metrics OpenFOAM's polyMesh is measured by."""
        return [c for c in cls if not c.value.startswith('gmsh')]

    @classmethod
    def gmsh(cls):
        """Gmsh's measures, in the order they are offered."""
        return [c for c in cls if c.value.startswith('gmsh')]

    @property
    def isGmsh(self) -> bool:
        return self.value.startswith('gmsh')


#: DP-714. Metrics whose bad cells are the low values: the tiny cells. Every
#: Gmsh measure is a quality in 0..1 (or a size) whose bad end is low.
QUALITY_WORST_IS_LOW = frozenset({MeshQualityIndex.VOLUME,
                                  *MeshQualityIndex.gmsh()})

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
    #: Plan 39 VI4 (F1e). How this actor looks changed in a way the patch skin
    #: cares about: shown or hidden, faded, selected, painted by a field,
    #: moved, cut, drawn another way. (A colour change is `colorChanged`.)
    appearanceChanged = Signal()

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
        self._mapper.SetLookupTable(colour_standard.lookup_table('quality', 0.0, 1.0))
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
        self._silhouetteFilter = self._newSilhouetteFilter()
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
        self._themeAccent = None
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

    @staticmethod
    def _newSilhouetteFilter():
        """The outline filter: boundary and feature edges, no colouring."""
        outline = vtkFeatureEdges()
        outline.BoundaryEdgesOn()
        outline.FeatureEdgesOn()
        outline.SetFeatureAngle(SILHOUETTE_FEATURE_ANGLE)
        outline.ManifoldEdgesOff()
        outline.NonManifoldEdgesOff()
        outline.ColoringOff()
        return outline

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
        self.appearanceChanged.emit()

    #: Plan 48 OP. Whether the route-wide Surface opacity multiplies this
    #: actor's own opacity (the model's surfaces do; markers and the mesh do not).
    ROUTE_OPACITY = False

    def effectiveOpacity(self) -> float:
        """What the prop is drawn with: its own opacity times the route's."""
        own = self._properties.opacity if self._properties is not None else None
        own = 1.0 if own is None else float(own)
        if self.ROUTE_OPACITY:
            active = activeSurfaceOpacity()
            if active is not None:
                return own * active
        return own

    def setOpacity(self, opacity):
        self._properties.opacity = opacity
        shown = self.effectiveOpacity()
        self._actor.GetProperty().SetOpacity(shown)
        # An outline at full strength around a surface faded to 20% reads as a
        # wireframe cage, so the outline fades with what it outlines.
        self._silhouette.GetProperty().SetOpacity(shown)
        self.appearanceChanged.emit()

    def applyRouteOpacity(self):
        """The route's Surface opacity changed: draw with it again."""
        if not self.ROUTE_OPACITY or self._properties is None:
            return
        shown = self.effectiveOpacity()
        if not getattr(self, '_skinned', False):
            self._actor.GetProperty().SetOpacity(shown)
        self._silhouette.GetProperty().SetOpacity(shown)
        self.appearanceChanged.emit()

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
                       lookup_table=None, magnitude: bool = False) -> bool:
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

        Plan 43 LY12. With ``magnitude`` the signed values stay in the array
        named ``name`` (the labels and the hover read them) and the colours
        follow ``|value|`` through a second array, so a surface pushed out and
        one pulled in by the same amount get the same colour.
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
        colour_name = name
        if magnitude:
            colour_name = name + MAGNITUDE_SUFFIX
            shown = numpy_to_vtk(np.abs(scalars), deep=True)
            shown.SetName(colour_name)
            data.GetCellData().AddArray(shown)
            data.GetCellData().SetActiveScalars(colour_name)

        if value_range is None:
            value_range = (float(finite.min()), float(finite.max()))
        low, high = (float(value_range[0]), float(value_range[1]))
        if lookup_table is not None:
            if magnitude:
                # The table may span a narrower window (Around the limit).
                low, high = lookup_table.GetTableRange()
            else:
                lookup_table.SetTableRange(low, high)
            self._mapper.SetLookupTable(lookup_table)
        self._mapper.SetScalarModeToUseCellFieldData()
        self._mapper.SelectColorArray(colour_name)
        self._mapper.SetScalarRange(low, high)
        if magnitude:
            self._mapper.UseLookupTableScalarRangeOn()
        else:
            self._mapper.UseLookupTableScalarRangeOff()
        self._mapper.ScalarVisibilityOn()
        self._mapper.Update()
        self._faceScalarName = name
        self.appearanceChanged.emit()
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
        self._mapper.SetLookupTable(colour_standard.lookup_table('quality', 0.0, 1.0))
        self._faceScalarName = None
        self._mapper.Update()
        self.appearanceChanged.emit()

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
        self.appearanceChanged.emit()

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
        self.appearanceChanged.emit()

    def setCutEnabled(self, enabled):
        self._properties.cutEnabled = enabled

    def setHighlighted(self, highlighted):
        if self._properties.highlighted != highlighted:
            self._properties.highlighted = highlighted
            if highlighted:
                self._highlightOn()
            else:
                self._highlightOff()
            self.appearanceChanged.emit()

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
        if role in ACCENT_ROLES:
            if app.themeManager is not None and app.themeManager.tokens is not None:
                self._highlightColor = _accentOr(
                    app.themeManager.tokens, self._highlightColor)
            elif self._themeAccent:
                self._highlightColor = self._themeAccent
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
        setThemeMeshLineColor(tokens.value('viewport.lines'))
        self._themeAccent = tokens.value('accent.default')
        if self._highlightRole in ACCENT_ROLES:
            self._highlightColor = self._themeAccent
        else:
            self._highlightColor = HIGHLIGHT_ROLE_COLORS[self._highlightRole]
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
            self._silhouette.GetProperty().SetColor(*rgb(self._highlightSilhouette()))
        else:
            self._actor.GetProperty().SetEdgeColor(*rgb(self._meshLineColor()))
            self._silhouette.GetProperty().SetColor(*rgb(self._silhouetteColor))
        # U1. The grid colour changed with the theme; wireframe skins and
        # edge modes pick it up from the same pass, with no reload.
        self.applyMeshLineStyle()

    def _highlightSilhouette(self) -> str:
        if self._highlightRole in ACCENT_ROLES:
            return HIGHLIGHT_SILHOUETTE_COLOR
        return self._highlightColor

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
        if self._staysUncut(planes):
            self.appearanceChanged.emit()
            return
        self._dropCutFilters()
        built, inputFilter = self._clipStage(planes, self._cutFilters[0])
        self._cutFilters.extend(built)
        self._connectMapper(inputFilter)
        self._mapper.Update()
        self.appearanceChanged.emit()

    def _staysUncut(self, planes) -> bool:
        """Plan 39 VI14c. Taking a cut off an actor that has none.

        Re-pointing a filter at the same input marks it modified, so the next
        pull re-runs it over the whole patch. A patch with no cut stages and
        nothing to cut by keeps its pipeline as it is.
        """
        return (len(self._cutFilters) == 1
                and not (planes and self._properties.cutEnabled))

    def _dropCutFilters(self):
        """Take the cut stages down, leaving the pass-through they hang on."""
        for i in reversed(range(1, len(self._cutFilters))):
            self._cutFilters[i].RemoveAllInputConnections(0)
            self._cutFilters.pop()

    def _clipStage(self, planes, inputFilter):
        """``(new stages, the last one)`` clipping *inputFilter* by *planes*.

        Plan 39 VI2. The stages are built apart from the actor's own chain so
        a volume can run them on the VTK thread and swap them in finished.
        """
        if planes and self._properties.cutEnabled:
            # Plan 37 UF7. Cut cells is one stage over all the planes (the
            # first is the one cutting, the rest mask), not a chain of clips.
            cells = (self._cutCellsFilter(planes)
                     if self._cutMode is CutMode.CUT_CELLS else None)
            # Plan 39 VI10b. How the planes combine and which whole cells
            # survive (None: the chain of single clips below, as before).
            from foammesh.rendering import combine_clip
            built = (combine_clip.stages(self, planes, inputFilter)
                     if cells is None else None)
            if built is not None:
                return list(built.filters), built.output
            stages = []
            for f in ([cells] if cells is not None
                      else [self._clipFilter(c) for c in planes]):
                f.SetInputConnection(inputFilter.GetOutputPort())
                stages.append(f)
                inputFilter = f
            return stages, inputFilter
        return [], inputFilter

    def slice(self, planes):
        """Cut the actor with one or more planes.

        A single plane used to be the only option, which made a boundary layer
        in a corner impossible to inspect: that needs two or three slices at
        once. ``None`` and a bare plane are still accepted, because that is what
        every existing caller passes.
        """
        if self._staysUncut(planes if planes is not None else []):
            self.appearanceChanged.emit()
            return
        self._dropCutFilters()
        built, inputFilter = self._sliceStage(planes, self._cutFilters[0])
        self._cutFilters.extend(built)
        self._connectMapper(inputFilter)
        self._mapper.Update()
        self.appearanceChanged.emit()

    def _sliceStage(self, planes, inputFilter):
        if planes is None:
            planes = []
        elif isinstance(planes, vtkPlane):
            planes = [planes]

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
                return [cutters[0]], cutters[0]
            union = vtkAppendPolyData()
            for f in cutters:
                union.AddInputConnection(f.GetOutputPort())
            return [*cutters, union], union
        return [], inputFilter

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
        self._silhouette.GetProperty().SetColor(*rgb(self._highlightSilhouette()))
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


#: Plan 39 VI16 (I9). How many times a volume's geometry filter (the
#: exterior surface of the cells) has run, in this process. The count is a
#: gate: a display-mode switch, a filter clear or a level-of-detail rebuild
#: after a load must add nothing, because the exterior of a revision is
#: worked out once and kept.
GEOMETRY_FILTER_RUNS = [0]


def geometryFilterRuns() -> int:
    return GEOMETRY_FILTER_RUNS[0]


def _watchGeometry(owner, geometry, outline=None):
    """Count the runs of *geometry* and keep what an uncut run produced.

    The observers hold the actor weakly: a filter that referred to the actor
    it hangs off would put a cycle in every volume (Plan 35 CR3 step 7).
    """
    import weakref

    ref = weakref.ref(owner)

    def ran(_caller, _event):
        GEOMETRY_FILTER_RUNS[0] += 1
        actor = ref()
        if actor is not None and outline is not None:
            actor._exteriorRan(geometry)

    geometry.AddObserver('EndEvent', ran)
    if outline is not None:
        def edgesRan(_caller, _event):
            actor = ref()
            if actor is not None:
                actor._edgesRan(outline)

        outline.AddObserver('EndEvent', edgesRan)


#: Closing round 2026-10-12 (VI14). While a section plane is being dragged,
#: a cut that has been worked out waits to go on screen: putting each one in
#: costs a render that uploads the whole new surface (about 100 ms for 1.9 M
#: faces on the integrated GPU), and only the last one of a drag is wanted.
_installHolds = 0
_HOLD_LIMIT_SECONDS = 10.0


def hold_cut_installs(hold: bool) -> None:
    global _installHolds
    _installHolds = max(0, _installHolds + (1 if hold else -1))


def cut_installs_held() -> bool:
    return _installHolds > 0


async def _installsAllowed() -> None:
    """Wait (at most ten seconds) until no plane drag is in progress."""
    waited = 0.0
    while _installHolds > 0 and waited < _HOLD_LIMIT_SECONDS:
        await asyncio.sleep(0.03)
        waited += 0.03


def _updateBoth(surface, outline) -> None:
    """Run a cut's two ends (the VTK thread)."""
    surface.Update()
    outline.Update()


def _releaseStages(stages) -> None:
    for stage in reversed(stages):
        try:
            stage.RemoveAllInputConnections(0)
        except Exception:                                   # noqa: BLE001
            pass


class _ShadowCut:
    """Plan 39 VI2. The volume's pipeline as a cut or a quality band would
    leave it, built apart from the one on screen and run on the VTK thread.

    The picture keeps drawing from the old pipeline until this one has been
    run to the end; a cancel throws this one away and nothing on screen
    changed.
    """

    def __init__(self, cell, cuts, surface, outline, precomputed):
        self.cell = cell
        self.cuts = cuts
        self.surface = surface
        self.outline = outline
        #: The uncut volume's surface and outline are handed over ready
        #: (CR3 step 10): nothing to run.
        self.precomputed = precomputed
        #: No cut and no band: the whole volume's exterior (Plan 39 VI16).
        self.uncut = False

    def _all(self):
        return [self.cell, *self.cuts, self.surface, self.outline]

    def update(self):
        if self.precomputed:
            return
        self.surface.Update()
        self.outline.Update()

    def abort(self):
        for stage in self._all():
            try:
                stage.SetAbortExecute(1)
            except Exception:                               # noqa: BLE001
                pass

    def release(self):
        for stage in reversed(self._all()):
            try:
                stage.RemoveAllInputConnections(0)
            except Exception:                               # noqa: BLE001
                pass


class MeshActor(ActorInfo):
    #: Plan 39 VI2. A cut or quality filter over the volume started (True)
    #: or has come to its end, swapped in or thrown away (False).
    cutBusy = Signal(bool)

    #: What the next pipeline is built from, and what the one on screen was.
    _cutSpec = None
    _cellSpec = None
    _shownSpecs = (None, None)
    _cutWanted = False
    _cutRunning = None
    _cutTask = None
    _cutGeneration = 0

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
        _watchGeometry(self, self._surfaceFilter, self._silhouetteFilter)

    # -- Plan 39 VI16 (I9): the exterior, once per revision --------------- #

    def _exteriorRan(self, geometry):
        """The pipeline's own geometry filter just ran: keep its exterior.

        The exterior of the uncut, unfiltered volume is the same polygons for
        every display mode, filter clear and level-of-detail rebuild of this
        revision, so it is kept as the precomputed surface (the worker's own
        takes the same place) and drawn from there. ``setDataSet`` drops it:
        a new revision has a new exterior. The mapper is not re-pointed here
        (this runs inside the pipeline's own update); the next cut or clear
        takes it from ``_precomputedSurface``.
        """
        if self._precomputedSurface is not None:
            return
        if geometry is not self._surfaceFilter or not self._isUncut():
            return
        output = geometry.GetOutput()
        if output is None or output.GetNumberOfCells() <= 0:
            return
        surface = vtkPolyData()
        surface.ShallowCopy(output)
        self._precomputedSurface = surface

    def _edgesRan(self, outline):
        """The outline filter ran over the whole exterior: keep its edges."""
        if self._precomputedEdges is not None or outline is not self._silhouetteFilter:
            return
        if self._precomputedSurface is None or not self._isUncut():
            return
        output = outline.GetOutput()
        if output is None:
            return
        edges = vtkPolyData()
        edges.ShallowCopy(output)
        self._precomputedEdges = edges

    def _isUncut(self) -> bool:
        return self._precomputedActive()

    def exteriorKept(self) -> bool:
        """Whether this revision's exterior is held (worker's or worked out)."""
        return self._precomputedSurface is not None

    # -- Plan 39 VI16 (I6): Colour mesh by, on the exterior ----------------- #

    _paintedSurface = None
    _paintColouring = None

    def exteriorSurfaceWith(self, arrays):
        """The volume's exterior with the cell arrays *arrays* on its faces.

        The worker's own exterior carries only ``patchId``; a face of the
        geometry filter's output takes its cell's data, so this is made over
        the whole dataset once and kept for the revision (``setDataSet`` drops
        it). Returns ``None`` when the dataset lacks an array.
        """
        data = self._dataSet
        if data is None or data.GetNumberOfCells() <= 0:
            return None
        cells = data.GetCellData()
        if any(cells.GetArray(name) is None for name in arrays):
            return None
        kept = self._paintedSurface
        if kept is not None and all(
                kept.GetCellData().GetArray(name) is not None
                for name in arrays):
            return kept
        geometry = vtkGeometryFilter()
        geometry.SetInputData(data)
        geometry.Update()
        surface = vtkPolyData()
        surface.ShallowCopy(geometry.GetOutput())
        self._paintedSurface = surface
        return surface

    def paintExterior(self, mapped, colouring) -> bool:
        """Colour the exterior by *mapped* (`section_colour.mapping`).

        Not while a cut or band is on: that picture is the section's, coloured
        by *Colour cut by*. Returns whether it was painted.
        """
        if not self._precomputedActive() or self.isCutting():
            return False
        surface = self.exteriorSurfaceWith([mapped['array']])
        if surface is None:
            return False
        self.dropSkin()
        self._mapper.SetInputData(surface)
        colouring.colour(self._mapper, mapped)
        self._paintColouring = colouring
        self._actor.Modified()
        self.appearanceChanged.emit()
        return True

    def exteriorPainted(self) -> bool:
        return self._paintColouring is not None

    def clearExteriorPaint(self) -> bool:
        """Take the colour off and draw the plain exterior again."""
        colouring = self._paintColouring
        if colouring is None:
            return False
        self._paintColouring = None
        colouring.restore(self._mapper)
        if self._precomputedActive():
            self._wirePrecomputed()
        self._actor.Modified()
        self.appearanceChanged.emit()
        return True

    # -- Plan 39 VI4 (F1e): the exterior wears the patches' colours --------- #

    _skinOn = False
    _skinLut = None
    _skinCount = None

    def canCarrySkin(self, count: int) -> bool:
        """Whether this exterior can be coloured by the patch each face is.

        It needs the worker's exterior with its ``patchId`` for *count*
        patches, shown, opaque, not selected, drawn as a surface, with nothing
        cutting, filtering or painting it.
        """
        surface = self._precomputedSurface
        props = self._properties
        if (surface is None or props is None or not props.visibility
                or self.effectiveOpacity() < 1.0 or props.highlighted
                or props.displayMode is DisplayMode.WIREFRAME
                or not self._precomputedActive() or self.isCutting()
                or self._paintColouring is not None
                or any(self._actor.GetPosition())):
            return False
        ids = surface.GetCellData().GetArray('patchId')
        if ids is None or ids.GetNumberOfTuples() != surface.GetNumberOfCells():
            return False
        low, high = ids.GetRange()
        return low >= 0 and high < count

    def carriesPatchIds(self) -> bool:
        """Whether the exterior on hand says which patch each face is."""
        surface = self._precomputedSurface
        return (surface is not None
                and surface.GetCellData().GetArray('patchId') is not None)

    def neutralSkinColour(self) -> tuple:
        colour = self._currentSurfaceColor()
        return (colour.redF(), colour.greenF(), colour.blueF())

    def skinApplied(self) -> bool:
        return self._skinOn

    def applySkin(self, colours) -> bool:
        """Colour the exterior's faces by ``patchId``: ``colours[i]`` is the
        RGB of patch *i*. The exterior's own mesh lines go (the patches draw
        theirs)."""
        from vtkmodules.vtkCommonCore import vtkLookupTable

        surface = self._precomputedSurface
        if surface is None or not colours:
            return False
        table = self._skinLut
        if table is None:
            table = self._skinLut = vtkLookupTable()
        table.SetNumberOfTableValues(len(colours))
        for index, (red, green, blue) in enumerate(colours):
            table.SetTableValue(index, red, green, blue, 1.0)
        table.SetRange(-0.5, len(colours) - 0.5)
        table.Modified()
        mapper = self._mapper
        mapper.SetInputData(surface)
        mapper.SetLookupTable(table)
        mapper.SetScalarModeToUseCellFieldData()
        mapper.SelectColorArray('patchId')
        mapper.UseLookupTableScalarRangeOn()
        mapper.ScalarVisibilityOn()
        self._skinOn = True
        self._actor.GetProperty().EdgeVisibilityOff()
        self._invalidateLevelOfDetail()
        return True

    def setSkinColour(self, index: int, colour) -> None:
        """Change the colour of one patch's faces on the exterior."""
        table = self._skinLut
        if table is None or not self._skinOn:
            return
        table.SetTableValue(index, colour[0], colour[1], colour[2], 1.0)
        table.Modified()
        self._actor.Modified()

    def dropSkin(self) -> None:
        """Draw the plain exterior again, mesh lines and all."""
        if not self._skinOn:
            return
        self._skinOn = False
        self._mapper.ScalarVisibilityOff()
        self._mapper.SetLookupTable(colour_standard.lookup_table('quality', 0.0, 1.0))
        self._mapper.SetScalarModeToUseCellFieldData()
        if self._properties.highlighted:
            self._highlightOn()
        else:
            self._applyDisplayMode(self._properties.displayMode)
        self._invalidateLevelOfDetail()

    def _applySurfaceEdgeMode(self):
        super()._applySurfaceEdgeMode()
        if self._skinOn:
            self._actor.GetProperty().EdgeVisibilityOff()

    # -- Plan 39 VI2: cuts and bands over the volume, off the GUI thread ----- #

    def clip(self, planes):
        self._cutSpec = ('clip', planes)
        self._submitCut()

    def slice(self, planes):
        self._cutSpec = ('slice', planes)
        self._submitCut()

    def recut(self):
        """Work the cut or band in force out again from the volume as it is.

        VI14 (hover) cuts a shallow copy of the volume taken when the cut is
        built, so an array put on the volume afterwards (a colour's cell id,
        a case's refinement level) is not on the surface until the cut is
        built again. Does nothing when nothing is cut.
        """
        if self._cutSpec is not None or self._cellSpec is not None:
            self._submitCut()

    def isCutting(self) -> bool:
        """Whether a cut or band is still being worked out (the picture on
        screen is the one before it)."""
        return self._cutRunning is not None or self._cutWanted

    def cancelCut(self) -> bool:
        """Stop the cut being worked out and keep the picture on screen.

        ``False`` when nothing was running.
        """
        if not self.isCutting():
            return False
        self._cutWanted = False
        self._cutGeneration += 1
        self._cutSpec, self._cellSpec = self._shownSpecs
        if self._cutRunning is not None:
            self._cutRunning.abort()
        self.appearanceChanged.emit()
        return True

    _originalIds = None

    def _ensureOriginalIds(self):
        """The array of cell indices, made once per revision.

        Plan 39 VI14c. 4 bytes a cell (10 ms on 6.6 M cells); ``_runCuts``
        makes it on a thread before the first cut is built, so the GUI
        thread does not.
        """
        data = self._dataSet
        count = int(data.GetNumberOfCells())
        ids = self._originalIds
        if ids is None or ids.GetNumberOfTuples() != count:
            from vtkmodules.util.numpy_support import numpy_to_vtk
            import numpy as np
            ids = numpy_to_vtk(np.arange(count, dtype=np.int32),
                               deep=True)
            ids.SetName('vtkOriginalCellIds')
            self._originalIds = ids
        return ids

    def _dataWithOriginalIds(self):
        """The volume, as a shallow copy that also carries each cell's own
        index as ``vtkOriginalCellIds``.

        VI14 (hover). A clip, cut or band keeps every cell array through to
        the surface it draws, so a cell picked on the cut can name the volume
        cell it stands for. The copy shares the volume's arrays (the volume
        itself is not given the array, so nothing that writes or lists it
        sees one); the ids are 4 bytes a cell and are kept for the revision.
        """
        data = self._dataSet
        ids = self._ensureOriginalIds()
        carrier = data.NewInstance()
        carrier.ShallowCopy(data)
        carrier.GetCellData().AddArray(ids)
        return carrier

    def _buildShadow(self) -> _ShadowCut:
        """The pipeline the specs ask for, as filters nothing has run yet."""
        if self._cellSpec is None:
            cell = vtkPassThrough()
        else:
            low, high, index = self._cellSpec
            cell = vtkThreshold()
            cell.AllScalarsOff()
            cell.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
            cell.SetLowerThreshold(low)
            cell.SetUpperThreshold(high)
            cell.SetInputArrayToProcess(
                0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, index.value)
        cell.SetInputData(self._dataWithOriginalIds())
        base = vtkPassThrough()
        base.SetInputConnection(cell.GetOutputPort())
        cuts, last = [base], base
        if self._cutSpec is not None:
            kind, planes = self._cutSpec
            stage = self._clipStage if kind == 'clip' else self._sliceStage
            built, last = stage(planes, base)
            cuts.extend(built)
        surface = vtkGeometryFilter()
        surface.SetInputConnection(last.GetOutputPort())
        outline = self._newSilhouetteFilter()
        outline.SetInputConnection(surface.GetOutputPort())
        # Plan 39 VI16 (I9). Handed over ready only once an exterior is kept;
        # until then the uncut pipeline is run here (off the GUI thread) and
        # its exterior is kept at install, so the next clear runs nothing.
        uncut = len(cuts) == 1 and self._cellSpec is None
        precomputed = uncut and self._precomputedSurface is not None
        shadow = _ShadowCut(cell, cuts, surface, outline, precomputed)
        shadow.uncut = uncut
        _watchGeometry(self, surface, outline)
        return shadow

    def _submitCut(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # No event loop to run it on (a test, a shutdown): worked out
            # here, as it always was.
            self.appearanceChanged.emit()
            shadow = self._buildShadow()
            shadow.update()
            self._installShadow(shadow)
            return
        self._cutWanted = True
        self.appearanceChanged.emit()
        if self._cutRunning is not None:
            # The latest request wins; the one running is of no use now.
            self._cutRunning.abort()
            return
        if self._cutTask is None or self._cutTask.done():
            self._cutTask = loop.create_task(self._runCuts())

    async def _runCuts(self):
        self.cutBusy.emit(True)
        try:
            while self._cutWanted and not self._disposed:
                self._cutWanted = False
                generation = self._cutGeneration
                specs = (self._cutSpec, self._cellSpec)
                if self._originalIds is None:
                    await asyncio.to_thread(self._ensureOriginalIds)
                    if self._disposed:
                        break
                shadow = self._buildShadow()
                self._cutRunning = shadow
                try:
                    await vtk_run_in_thread(shadow.update)
                except Exception:                           # noqa: BLE001
                    logging.getLogger(__name__).exception(
                        'cut of %s not worked out', self._id)
                    shadow.release()
                    continue
                finally:
                    self._cutRunning = None
                await _installsAllowed()
                if (self._cutWanted or self._disposed
                        or generation != self._cutGeneration):
                    shadow.release()
                    continue
                self._installShadow(shadow, specs)
        finally:
            self._cutRunning = None
            if not self._disposed:
                self.cutBusy.emit(False)

    def _installShadow(self, shadow: _ShadowCut, specs=None):
        """Put a finished pipeline on screen in place of the old one."""
        old = [self._cellFilter, *self._cutFilters, self._surfaceFilter,
               self._silhouetteFilter]
        self._cellFilter = shadow.cell
        self._cutFilters = shadow.cuts
        self._surfaceFilter = shadow.surface
        self._silhouetteFilter = shadow.outline
        self._shownSpecs = specs or (self._cutSpec, self._cellSpec)
        if shadow.uncut and not shadow.precomputed                 and self._precomputedSurface is None:
            # Plan 39 VI16 (I9). Worked out here once; every later clear and
            # mode switch of this revision draws this.
            kept = vtkPolyData()
            kept.ShallowCopy(shadow.surface.GetOutput())
            if kept.GetNumberOfCells() > 0:
                self._precomputedSurface = kept
                edges = vtkPolyData()
                edges.ShallowCopy(shadow.outline.GetOutput())
                self._precomputedEdges = edges
        self._mapper.SetInputConnection(shadow.surface.GetOutputPort())
        self._silhouetteMapper.SetInputConnection(
            shadow.outline.GetOutputPort())
        self._wirePrecomputed()
        # The cell state below turns the mapper's colouring off.
        self._skinOn = False
        self._applyCellState()
        self._mapper.Update()
        self._invalidateLevelOfDetail()
        for stage in reversed(old):
            stage.RemoveAllInputConnections(0)
        self.appearanceChanged.emit()

    def _applyCellState(self):
        """The mapper's colouring and depth bias for the band on screen."""
        if self._cellSpec is None:
            self._mapper.ScalarVisibilityOff()
            # DP-714. The band's scale was lent for the colouring only.
            self._mapper.SetLookupTable(colour_standard.lookup_table('quality', 0.0, 1.0))
            # Whatever suspended the standing bias is off too, so it comes
            # back.
            self._applyDepthBias(self._depthBias)
            return
        low, high, index = self._cellSpec
        self._mapper.ScalarVisibilityOn()
        # DP-714. A scale of its own on the band, not the shared table's 0-1
        # range, which painted every value above 1 the same red.
        table = colour_standard.lookup_table(
            'quality', low, high, index not in QUALITY_WORST_IS_LOW,
            limit=quality_scale.limit(index.value))
        self._mapper.SetLookupTable(table)
        self._mapper.SetScalarRange(*table.GetTableRange())
        self._mapper.UseLookupTableScalarRangeOn()
        self._mapper.SelectColorArray(index.value)
        # A thresholded volume is no longer a duplicate of its own skin -- it
        # is the subset of cells the user asked to be shown, and asking to see
        # the worst cells and then being shown the patches that cover them is
        # not an answer. So the bias that keeps the whole volume behind its
        # patches (DP-141) stands down for as long as the filter is on --
        # and turns round (DP-737): at zero a boundary cell's face tied with
        # the patch face over it and shimmered through it, so the cells are
        # pulled in front instead. clearCellFilter puts the standing bias back.
        self._applyDepthBias(POOR_CELL_DEPTH_BIAS)

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
        self._paintedSurface = None
        self._skinOn = False
        if self._paintColouring is not None:
            self._paintColouring.restore(self._mapper)
            self._paintColouring = None
        # Plan 39 VI2. A cut worked out over the old data is of no use.
        self._cutGeneration += 1
        self._originalIds = None
        if self._cutRunning is not None:
            self._cutRunning.abort()
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
        # WALK-2. A Python algorithm (the Cut cells filter) has no
        # GetOutput(); every vtkAlgorithm has GetOutputDataObject.
        return cut.GetOutputDataObject(0).GetNumberOfCells()

    def hasScalar(self, index: MeshQualityIndex) -> bool:
        return self._dataSet.GetCellData().GetScalars(index.value) is not None

    def getScalarRange(self, index: MeshQualityIndex) -> tuple[float, float]:
        # print(f'Name: {self.name()} Field: {index.value}')
        scalars = self._dataSet.GetCellData().GetScalars(index.value)
        if scalars is None:
            return 0, 1

        left, right = scalars.GetRange()
        return left, right

    def metricValues(self, index: MeshQualityIndex):
        """Plan 43 LY11. The per-cell values of a metric, or None if absent.

        Read from the arrays already attached; ``nan`` marks a cell the
        metric was not measured on. The legend's statistics come from here.
        """
        import numpy as np
        from vtkmodules.util.numpy_support import vtk_to_numpy

        data = self._dataSet
        array = data.GetCellData().GetArray(index.value) if data else None
        if array is None:
            return None
        return vtk_to_numpy(array).astype(np.float64, copy=True)

    def rankedCells(self, index: MeshQualityIndex, count: int) -> list[dict]:
        """The ``count`` worst cells of a metric on this volume, worst first.

        Plan 39 VI8. Read from the arrays already attached, so asking costs a
        partition and not a second measurement. Each row is what the Quality
        tab shows and the camera needs: id, value, centroid and a length for
        the cell. The zone is not known to a dataset, so it is left blank here;
        the worker's ranking names it.
        """
        import numpy as np
        from vtkmodules.util.numpy_support import vtk_to_numpy
        from vtkmodules.vtkCommonCore import vtkIdList

        from foammesh.core.quality.cell_fields import rank_cells

        data = self._dataSet
        array = data.GetCellData().GetArray(index.value) if data else None
        if array is None:
            return []
        values = vtk_to_numpy(array).astype(np.float64, copy=False)
        cells = rank_cells(values, count,
                           worst_is_high=index not in QUALITY_WORST_IS_LOW)
        volumes = data.GetCellData().GetArray(MeshQualityIndex.VOLUME.value)
        if volumes is None:
            volumes = data.GetCellData().GetArray(
                MeshQualityIndex.GMSH_VOLUME.value)
        volume = vtk_to_numpy(volumes) if volumes is not None else None
        points = vtkIdList()
        # Plan 42 SG14 (DP-1419). On a Gmsh mesh the row's tag is the
        # element's Gmsh tag, which Save listed cells keeps; the dataset
        # position is the tag only on a mesh that carries no tags.
        tag_array = data.GetCellData().GetArray('gmshElementTag')
        tags = vtk_to_numpy(tag_array) if tag_array is not None else None
        rows = []
        for cell in cells:
            data.GetCellPoints(int(cell), points)
            ids = [points.GetId(i) for i in range(points.GetNumberOfIds())]
            if ids:
                coordinates = np.array([data.GetPoint(i) for i in ids])
                centroid = coordinates.mean(axis=0)
                extent = float(np.ptp(coordinates, axis=0).max())
            else:
                centroid, extent = np.zeros(3), 0.0
            size = (float(np.cbrt(abs(volume[cell])))
                    if volume is not None else extent)
            rows.append({'tag': str(int(tags[cell]) if tags is not None
                                    else int(cell)),
                         'cell': int(cell),
                         'metric': index.value, 'value': float(values[cell]),
                         'centroid': [float(x) for x in centroid],
                         'size': size, 'zone': ''})
        return rows

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
        self._cellSpec = None
        self._submitCut()

    def applyCellFilter(self):
        self._cellSpec = (self._mqLow, self._mqHigh, self._mqIndex)
        self._submitCut()


class BoundaryActor(ActorInfo):
    #: Closing round 2026-10-12 (VI14). A patch of at least this many faces is
    #: cut on the VTK thread, as the volume is: the cut of one patch of the
    #: 6.6 M-cell impeller held the GUI thread for about 100 ms, and a
    #: section plane moved it once per step.
    CUT_OFF_THREAD_CELLS = 20000

    _boundarySpec = None
    _boundaryRunning = None
    _boundaryTask = None
    _boundaryGeneration = 0

    def __init__(self, dataSet, id_, name):
        super().__init__(dataSet, id_, name, ActorType.BOUNDARY)

    # -- VI14: a large patch's cut, off the GUI thread ------------------------ #

    def clip(self, planes):
        if not self._cutOffThread(planes):
            self._boundaryCancel()
            super().clip(planes)
            return
        self._boundarySubmit(('clip', planes))

    def slice(self, planes):
        if isinstance(planes, vtkPlane):
            planes = [planes]
        if not self._cutOffThread(planes):
            self._boundaryCancel()
            super().slice(planes)
            return
        self._boundarySubmit(('slice', planes))

    def _cutOffThread(self, planes) -> bool:
        data = self._dataSet
        if (not planes or data is None or self._disposed
                or self._properties is None
                or not self._properties.cutEnabled
                or data.GetNumberOfCells() < self.CUT_OFF_THREAD_CELLS):
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def isCutting(self) -> bool:
        """Whether a cut of this patch is still being worked out."""
        return self._boundaryRunning is not None or self._boundarySpec is not None

    def _boundaryCancel(self) -> None:
        self._boundarySpec = None
        self._boundaryGeneration += 1
        running = self._boundaryRunning
        if running is not None:
            for stage in running[0]:
                try:
                    stage.SetAbortExecute(1)
                except Exception:                           # noqa: BLE001
                    pass

    def _boundarySubmit(self, spec) -> None:
        self._boundarySpec = spec
        self._boundaryGeneration += 1
        running = self._boundaryRunning
        if running is not None:
            for stage in running[0]:
                try:
                    stage.SetAbortExecute(1)
                except Exception:                           # noqa: BLE001
                    pass
            return
        if self._boundaryTask is None or self._boundaryTask.done():
            self._boundaryTask = asyncio.get_running_loop().create_task(
                self._runBoundaryCuts())

    async def _runBoundaryCuts(self):
        while self._boundarySpec is not None and not self._disposed:
            kind, planes = self._boundarySpec
            self._boundarySpec = None
            generation = self._boundaryGeneration
            base = vtkPassThrough()
            base.SetInputData(self._dataSet)
            stage = self._clipStage if kind == 'clip' else self._sliceStage
            built, last = stage(planes, base)
            outline = self._newSilhouetteFilter()
            outline.SetInputConnection(last.GetOutputPort())
            stages = [base, *built, outline]
            self._boundaryRunning = (stages, base, built, last, outline)
            try:
                await vtk_run_in_thread(_updateBoth, last, outline)
                await _installsAllowed()
            except Exception:                               # noqa: BLE001
                logging.getLogger(__name__).exception(
                    'cut of %s not worked out', self._id)
                _releaseStages(stages)
                continue
            finally:
                self._boundaryRunning = None
            if (self._boundarySpec is not None or self._disposed
                    or generation != self._boundaryGeneration):
                _releaseStages(stages)
                continue
            self._installBoundaryCut(base, built, last, outline)

    def _installBoundaryCut(self, base, built, last, outline) -> None:
        """Put a cut that has been worked out on screen in place of the old."""
        oldOutline = self._silhouetteFilter
        self._dropCutFilters()
        self._cutFilters.extend([base, *built])
        self._silhouetteFilter = outline
        self._mapper.SetInputConnection(last.GetOutputPort())
        self._wirePrecomputed()
        oldOutline.RemoveAllInputConnections(0)
        self._mapper.Update()
        self.appearanceChanged.emit()

    def _initMapper(self) -> vtkPolyDataMapper:
        return vtkPolyDataMapper()

    # -- Plan 39 VI4 (F1e): edges only over the volume's exterior ------------ #

    _skinned = False

    def isSkinned(self) -> bool:
        """Whether this patch draws only its edges, its faces being drawn by
        the volume's exterior in its colour."""
        return self._skinned

    def isPlain(self) -> bool:
        """Whether the exterior can draw this patch's faces for it.

        Shown, opaque, not selected, not painted by a field, not moved, not
        cut, and drawn as a surface (with or without edges). Anything else is
        a cue only the patch's own faces can give.
        """
        props = self._properties
        return bool(
            props is not None and props.visibility
            and self.effectiveOpacity() >= 1.0
            and not props.highlighted
            and props.displayMode is not DisplayMode.WIREFRAME
            and self._faceScalarName is None
            and self._precomputedActive()
            and not any(self._actor.GetPosition()))

    def skinColour(self) -> tuple:
        colour = self._currentSurfaceColor()
        return (colour.redF(), colour.greenF(), colour.blueF())

    def setSkinned(self, skinned: bool) -> None:
        skinned = bool(skinned)
        if skinned == self._skinned:
            return
        self._skinned = skinned
        prop = self._actor.GetProperty()
        # A skinned patch is not what a click lands on: the exterior is, and
        # the picker hands the click back to the patch (`MeshManager`).
        self._actor.SetPickable(not skinned)
        if not skinned:
            prop.SetLighting(True)
            prop.SetOpacity(self.effectiveOpacity())
            self._actor.SetVisibility(self._properties.visibility)
            colour = self._currentSurfaceColor()
            prop.SetColor(colour.redF(), colour.greenF(), colour.blueF())
        if self._properties.highlighted:
            self._highlightOn()
        else:
            self._applyDisplayMode(self._properties.displayMode)

    def _applySkinLines(self):
        """Only this patch's mesh lines, as the surface-with-edges mode drew
        them: same colour, width and strength, no faces."""
        style = meshLineStyle()
        prop = self._actor.GetProperty()
        width, opacity = style.width, style.opacity
        if self._dataSet.GetNumberOfCells() > DENSE_SKIN_FACES:
            # DP-2675: see DENSE_SKIN_FACES.
            width = min(width, MESH_LINE_DEFAULT_WIDTH)
            opacity = min(opacity, EDGE_OPACITY)
        prop.SetRepresentationToWireframe()
        prop.EdgeVisibilityOff()
        prop.SetLighting(False)
        prop.SetColor(*rgb(self._meshLineColor()))
        prop.SetLineWidth(width)
        # Without per-edge opacity (VTK < 9.3) the edges were solid.
        prop.SetOpacity(opacity if hasattr(prop, 'SetEdgeOpacity') else 1.0)
        self._actor.SetVisibility(self._properties.visibility)
        self._silhouette.SetVisibility(self._properties.visibility)

    def _applySurfaceMode(self):
        super()._applySurfaceMode()
        if self._skinned:
            self._actor.SetVisibility(False)

    def _applySurfaceEdgeMode(self):
        if self._skinned:
            self._applySkinLines()
            return
        super()._applySurfaceEdgeMode()

    def setColor(self, color: QColor):
        super().setColor(color)
        if self._skinned:
            self._applyDisplayMode(self._properties.displayMode)

    def _applySurfaceColor(self):
        super()._applySurfaceColor()
        if self._skinned:
            self._applyDisplayMode(self._properties.displayMode)

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
    #: Plan 48 OP. The model's own surfaces take the route's Surface opacity.
    ROUTE_OPACITY = True

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
            # DP-2711: ``data`` is what the mapper draws, and vtkFeatureEdges
            # builds links/cells on its input, so it works on a private
            # shallow copy (own link/cell bookkeeping, shared read-only
            # arrays) taken here on the GUI thread.  Nothing a paint touches
            # is mutated, so the job needs no rendering hold; the result is
            # attached below, back on the GUI thread.
            private = vtkPolyData()
            private.ShallowCopy(data)
            edges = await vtk_run_in_background(feature_edges, private)
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
