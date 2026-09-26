"""How the viewport draws: one Render quality preset instead of loose switches.

render0925. The viewport's look was set in three places that did not know
about each other: ``RenderingWidget.__init__`` fixed 8x multisampling and an
untuned ``vtkLightKit``, ``setOrderIndependentTransparency`` dropped the
multisampling to zero whenever anything translucent was on screen, and the
View menu carried separate SSAO and FXAA check boxes that defaulted off.

The second of those is the one a user saw. Imported geometry is drawn at 0.9
opacity (``GeometryActor``), so the geometry view -- the first thing every
case shows -- always ran with depth peeling, which meant no anti-aliasing at
all: stair-stepped outlines on every fin and edge. FXAA works alongside depth
peeling, so it now fills the gap MSAA leaves.

Everything here takes a renderer and a render window rather than the Qt
widget, so the same code runs in the app and in an offscreen evidence render
(``plans/evidence/viewport-audit-20260925/render_quality_driver.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

#: Order is the order of the menu.
PRESETS = ('performance', 'balanced', 'quality')
DEFAULT_PRESET = 'balanced'

#: Depth peeling for translucent parts. Peeling and multisampling cannot be
#: combined on the OpenGL2 backend, so while it runs MSAA is off.
DEPTH_PEELS = 8
DEPTH_PEEL_OCCLUSION = 0.05

#: Screen-space ambient occlusion, sized from the model rather than in world
#: units so it reads the same on a 5 mm part and a 50 m domain.
SSAO_RADIUS_FRACTION = 0.05
SSAO_BIAS_FRACTION = 0.01
SSAO_KERNEL_SIZE = 32


@dataclass(frozen=True)
class RenderQuality:
    """What one preset turns on.

    There is no FXAA switch: FXAA runs exactly when the frame has no
    multisampling (see :func:`antiAliasing`).
    """
    name: str
    multiSamples: int
    ssao: bool = False
    peels: int = DEPTH_PEELS
    occlusion: float = DEPTH_PEEL_OCCLUSION


#: MEASURED (render0925, VTK 9.5.2, AMD Radeon, 1600x900, median of 3x60
#: frames; see plans/evidence/viewport-audit-20260925/render-after/README.md).
QUALITIES = {
    # FXAA instead of 8x MSAA: 40-50 % less frame time on 100k-200k cells.
    # Edge error against a 16-sample reference is about twice MSAA's
    # (2.4-3.0 against 1.3-1.4 grey levels) and half of no smoothing (5.2-5.5).
    # Four peels drew the translucent heat sink pixel-identical to 64 and
    # 23 % faster than eight.
    'performance': RenderQuality('performance', 0, peels=4, occlusion=0.1),
    # What the viewport always drew for an opaque scene (8x MSAA), now with
    # FXAA while depth peeling has switched MSAA off.
    'balanced': RenderQuality('balanced', 8),
    # Cavity shading where parts meet and passages turn. VTK's SSAO pass
    # draws into a single-sample buffer, so multisampling does nothing under
    # it (measured edge error 6.1-6.3 at 8x MSAA, worse than no smoothing)
    # and FXAA smooths the edges instead (2.9-3.3). It also draws the
    # background in one colour, the gradient's lower end. 1.4-1.7x Balanced's
    # frame time. Not the default.
    'quality': RenderQuality('quality', 0, ssao=True),
}

#: What the menu says each preset costs, from the measurement above.
LABELS = {
    'performance': ('&Performance',
                    'Edge smoothing by FXAA instead of multisampling. '
                    'About 40% faster on large meshes.'),
    'balanced': ('&Balanced',
                 '8x multisampling; FXAA while anything is see-through. '
                 'The default.'),
    'quality': ('&Quality',
                'Adds cavity shading where parts meet; edges smoothed by '
                'FXAA. About 50% slower, and the background becomes '
                'one colour.'),
}

#: The light kit, tuned. VTK's defaults (key 0.75, key:fill 3, key:head 3,
#: key:back 3.5, warmth 0.6/0.4) light a curved surface almost evenly; a
#: slightly stronger key and rim with a softer fill give it form. MEASURED on
#: the render0925 scenes: the 5th-95th percentile luminance spread over the
#: model grows 98 -> 107 (S5 pipe), 71 -> 79 (heat sink), 87 -> 98 (G6 tee),
#: with the median brighter by 5-9 levels and no clipped highlights.
LIGHT_KIT = {
    'KeyLightIntensity': 0.9,
    'KeyToFillRatio': 3.5,
    'KeyToHeadRatio': 6.0,
    'KeyToBackRatio': 2.5,
    'KeyLightWarmth': 0.55,
    'FillLightWarmth': 0.5,
}


def quality(name) -> RenderQuality:
    """The preset called ``name`` (or the preset itself); Balanced if unknown."""
    if isinstance(name, RenderQuality):
        return name
    return QUALITIES.get(str(name or '').lower(), QUALITIES[DEFAULT_PRESET])


def antiAliasing(preset, peeling: bool) -> tuple[int, bool]:
    """``(multiSamples, fxaa)`` for ``preset`` with or without depth peeling.

    FXAA runs exactly when there is no multisampling. The two together draw
    NOTHING on this backend -- the whole frame comes out black, measured
    offscreen with and without SSAO -- so they are never combined; and a
    frame with neither is the stair-stepped geometry view render0925 fixed.
    Depth peeling and SSAO both rule multisampling out.
    """
    chosen = quality(preset)
    samples = 0 if peeling or chosen.ssao else chosen.multiSamples
    return samples, samples == 0


def tuneLightKit(kit) -> None:
    for name, value in LIGHT_KIT.items():
        setter = getattr(kit, f'Set{name}', None)
        if setter is not None:
            setter(value)


def applyAntiAliasing(renderer, window, preset, peeling: bool) -> bool:
    samples, fxaa = antiAliasing(preset, peeling)
    window.SetMultiSamples(samples)
    if hasattr(renderer, 'SetUseFXAA'):
        renderer.SetUseFXAA(fxaa)
        return fxaa
    return False


def applyTransparency(renderer, window, preset, enabled: bool) -> bool:
    """Depth peeling on or off, and the anti-aliasing that goes with it.

    Returns whether FXAA is now on.
    """
    if enabled:
        window.SetAlphaBitPlanes(1)
        renderer.SetUseDepthPeeling(True)
        chosen = quality(preset)
        renderer.SetMaximumNumberOfPeels(chosen.peels)
        renderer.SetOcclusionRatio(chosen.occlusion)
    else:
        renderer.SetUseDepthPeeling(False)
    return applyAntiAliasing(renderer, window, preset, enabled)


def applyAmbientOcclusion(renderer, enabled: bool, extent: float) -> bool:
    """Cavity shading; returns whether this VTK build has it."""
    if not hasattr(renderer, 'SetUseSSAO'):
        return False
    enabled = bool(enabled)
    renderer.SetUseSSAO(enabled)
    if enabled:
        radius = float(extent or 0.0) * SSAO_RADIUS_FRACTION or 0.1
        renderer.SetSSAORadius(radius)
        renderer.SetSSAOBias(radius * SSAO_BIAS_FRACTION)
        renderer.SetSSAOKernelSize(SSAO_KERNEL_SIZE)
        renderer.SetSSAOBlur(True)
    return True


def modelExtent(renderer) -> float:
    bounds = renderer.ComputeVisiblePropBounds()
    if bounds[0] > bounds[1]:
        return 0.0
    return max(bounds[1] - bounds[0], bounds[3] - bounds[2],
               bounds[5] - bounds[4])


def applyRenderQuality(renderer, window, preset, *, lightKit=None,
                       translucent=False, extent=None) -> RenderQuality:
    """Put the whole preset on one renderer. Returns the preset applied."""
    chosen = quality(preset)
    if lightKit is not None:
        tuneLightKit(lightKit)
    applyTransparency(renderer, window, chosen, translucent)
    applyAmbientOcclusion(
        renderer, chosen.ssao,
        modelExtent(renderer) if extent is None else extent)
    return chosen
