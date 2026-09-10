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


def apply_vtk_theme(renderer, tokens, *, cube_axes=None, origin_axes=None, logo=None):
    """Apply background, annotation contrast, and watermark weight at runtime."""
    renderer.SetBackground(*rgb(tokens.value('viewport.top')))
    renderer.SetBackground2(*rgb(tokens.value('viewport.bottom')))
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
        logo.GetImageProperty().SetOpacity(
            0.20 if luminance(tokens.value('viewport.top')) > 0.55 else 0.34)
