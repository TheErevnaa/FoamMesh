"""VTK-facing application of the semantic FoamMesh theme tokens."""
from __future__ import annotations


def rgb(value: str) -> tuple[float, float, float]:
    """Convert a validated ``#rrggbb`` theme token to VTK floats."""
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))  # type: ignore[return-value]


def luminance(value: str) -> float:
    red, green, blue = rgb(value)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def apply_text_property(prop, color: str) -> None:
    if prop is not None:
        prop.SetColor(*rgb(color))


def apply_scalar_bar_theme(scalar_bar, tokens) -> None:
    annotation = tokens.value('foreground.primary')
    apply_text_property(scalar_bar.GetLabelTextProperty(), annotation)
    apply_text_property(scalar_bar.GetTitleTextProperty(), annotation)
    if hasattr(scalar_bar, 'GetAnnotationTextProperty'):
        apply_text_property(scalar_bar.GetAnnotationTextProperty(), annotation)


#: Plan 44 MF15. The mockup's ``radial-gradient(ellipse at 55% 35%, centre,
#: mid 55%, edge 100%)``. VTK's own radial mode is centred only, so the
#: gradient is a generated texture.
BACKGROUND_TEXTURE_SIZE = 512
BACKGROUND_CENTRE_AT = (0.55, 0.35)        # from the left, from the TOP
BACKGROUND_MID_STOP = 0.55
#: Watermark opacity (Q9): dark / light theme.
WATERMARK_OPACITY_DARK = 0.12
WATERMARK_OPACITY_LIGHT = 0.10

_texture_cache: dict = {}


def radial_background_array(centre: str, mid: str, edge: str,
                            size: int = BACKGROUND_TEXTURE_SIZE):
    """A ``size`` x ``size`` RGB uint8 array, row 0 at the BOTTOM (VTK order).

    CSS ``ellipse`` with the default ``farthest-corner`` extent: the ellipse
    has the proportions of the farthest-side ellipse and is scaled to pass
    through the farthest corner. Dithered with +-0.5 level of noise so the
    wide dark ramp does not band.
    """
    import numpy as np
    cx, cy = BACKGROUND_CENTRE_AT
    xs = (np.arange(size) + 0.5) / size
    ys_top = 1.0 - (np.arange(size) + 0.5) / size       # row 0 = bottom
    dx = (xs[None, :] - cx) / max(cx, 1.0 - cx)
    dy = (ys_top[:, None] - cy) / max(cy, 1.0 - cy)
    d = np.sqrt(dx * dx + dy * dy) / np.sqrt(2.0)
    d = np.clip(d, 0.0, 1.0)
    c0, c1, c2 = (np.array(rgb(c)) * 255.0 for c in (centre, mid, edge))
    t1 = np.clip(d / BACKGROUND_MID_STOP, 0.0, 1.0)[..., None]
    t2 = np.clip((d - BACKGROUND_MID_STOP) / (1.0 - BACKGROUND_MID_STOP),
                 0.0, 1.0)[..., None]
    inner = c0 + (c1 - c0) * t1
    image = np.where(d[..., None] < BACKGROUND_MID_STOP, inner,
                     c1 + (c2 - c1) * t2)
    noise = np.random.default_rng(15).uniform(-0.5, 0.5, image.shape)
    return np.clip(np.rint(image + noise), 0, 255).astype(np.uint8)


def radial_background_texture(centre: str, mid: str, edge: str):
    """A ``vtkTexture`` of the radial background, cached per colour triple."""
    key = (centre, mid, edge)
    texture = _texture_cache.get(key)
    if texture is not None:
        return texture
    from vtkmodules.vtkCommonCore import VTK_UNSIGNED_CHAR
    from vtkmodules.vtkCommonDataModel import vtkImageData
    from vtkmodules.vtkRenderingCore import vtkTexture
    from vtkmodules.util.numpy_support import numpy_to_vtk
    array = radial_background_array(centre, mid, edge)
    size = array.shape[0]
    image = vtkImageData()
    image.SetDimensions(size, size, 1)
    scalars = numpy_to_vtk(array.reshape(-1, 3), deep=True,
                           array_type=VTK_UNSIGNED_CHAR)
    scalars.SetName('background')
    image.GetPointData().SetScalars(scalars)
    texture = vtkTexture()
    texture.SetInputData(image)
    texture.InterpolateOn()
    texture.MipmapOff()
    texture.RepeatOff()
    texture.EdgeClampOn()
    if len(_texture_cache) > 8:
        _texture_cache.clear()
    _texture_cache[key] = texture
    return texture


def apply_background_texture(renderer, tokens) -> bool:
    """Paint the token radial background, if this renderer can show it."""
    if not hasattr(renderer, 'SetBackgroundTexture'):
        return False
    try:
        texture = radial_background_texture(
            tokens.value('viewport.centre'), tokens.value('viewport.mid'),
            tokens.value('viewport.edge'))
    except Exception:                                      # noqa: BLE001
        return False
    renderer.SetBackgroundTexture(texture)
    renderer.TexturedBackgroundOn()
    return True


def apply_triad_theme(cube, axes, tokens) -> None:
    """Recolour the viewport triad from ``triad.*`` (MF15)."""
    try:
        for name in ('triad.fill', 'triad.stroke', 'triad.x', 'triad.y', 'triad.up'):
            tokens.value(name)
    except (KeyError, AttributeError):
        return                       # a theme without the triad tokens
    if cube is not None:
        fill = rgb(tokens.value('triad.fill'))
        stroke = rgb(tokens.value('triad.stroke'))
        cube.GetCubeProperty().SetColor(*fill)
        cube.GetTextEdgesProperty().SetColor(*stroke)
        cube.GetCubeProperty().SetEdgeColor(*stroke)
        for face in ('X', 'Y', 'Z'):
            for sign in ('Plus', 'Minus'):
                getattr(cube, f'Get{face}{sign}FaceProperty')().SetColor(*fill)
    if axes is not None:
        colours = [rgb(tokens.value(name))
                   for name in ('triad.x', 'triad.y', 'triad.up')]
        for shaft, tip, colour in zip(
                (axes.GetXAxisShaftProperty(), axes.GetYAxisShaftProperty(),
                 axes.GetZAxisShaftProperty()),
                (axes.GetXAxisTipProperty(), axes.GetYAxisTipProperty(),
                 axes.GetZAxisTipProperty()), colours):
            shaft.SetColor(*colour)
            tip.SetColor(*colour)


def watermark_opacity(tokens) -> float:
    """.12 on the dark theme, .10 on the light one (Q9).

    The watermark sits bottom-right, which on the radial background is the
    ``viewport.edge`` end; ``viewport.bottom`` is the older name for it.
    """
    for name in ('viewport.edge', 'viewport.bottom'):
        try:
            ground = tokens.value(name)
            break
        except (KeyError, AttributeError):
            continue
    else:
        return WATERMARK_OPACITY_DARK
    return (WATERMARK_OPACITY_LIGHT if luminance(ground) > 0.5
            else WATERMARK_OPACITY_DARK)


def apply_vtk_theme(renderer, tokens, *, cube_axes=None, origin_axes=None, logo=None):
    """Apply background, annotation contrast, and watermark weight at runtime."""
    # DP-190. With GradientBackgroundOn, VTK paints Background at the BOTTOM
    # of the viewport and Background2 at the top. The token named for an end
    # of the gradient therefore goes into the slot that paints that end.
    renderer.SetBackground(*rgb(tokens.value('viewport.bottom')))
    renderer.SetBackground2(*rgb(tokens.value('viewport.top')))
    apply_background_texture(renderer, tokens)
    annotation = rgb(tokens.value('foreground.secondary'))
    if cube_axes is not None:
        axis_colours = [tokens.value(name) for name in
                        ('status.error', 'status.success', 'status.info')]
        for index in range(3):
            cube_axes.GetLabelTextProperty(index).SetColor(*annotation)
            cube_axes.GetTitleTextProperty(index).SetColor(*rgb(axis_colours[index]))
        cube_axes.GetXAxesLinesProperty().SetColor(*annotation)
        cube_axes.GetYAxesLinesProperty().SetColor(*annotation)
        cube_axes.GetZAxesLinesProperty().SetColor(*annotation)
    if origin_axes is not None:
        axis_colours = [tokens.value(name) for name in
                        ('status.error', 'status.success', 'status.info')]
        for shaft, colour in zip((origin_axes.GetXAxisShaftProperty(),
                                  origin_axes.GetYAxisShaftProperty(),
                                  origin_axes.GetZAxisShaftProperty()), axis_colours):
            shaft.SetColor(*rgb(colour))
        for tip, colour in zip((origin_axes.GetXAxisTipProperty(),
                                origin_axes.GetYAxisTipProperty(),
                                origin_axes.GetZAxisTipProperty()), axis_colours):
            tip.SetColor(*rgb(colour))
    if logo is not None:
        logo.GetImageProperty().SetOpacity(watermark_opacity(tokens))
