#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Labels drawn at the picture's size (Plan 43 LY15, section 5).

MEASURED (VTK 9.5.2, offscreen): ``vtkWindowToImageFilter.SetScale(4)`` tiles
the window and magnifies the geometry, but a ``vtkTextActor`` is drawn once at
its screen size -- a 4x PNG carried the legend text at 1x height, in one tile.
So the legend, the numbers on the model, the selected-cell tag and the caption
are not left to the tiling.  For a magnified picture they are lifted out of
the viewport's renderer, drawn by a second offscreen window that is exactly
the picture's size (same camera, fonts and offsets multiplied by the scale),
and composited over the tiled geometry.  The text is therefore rasterised at
its final size, in the same font engine the screen uses, and needs no Qt.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

_LABEL_CLASSES = ('vtkScalarBarActor', 'vtkTextActor', 'vtkBillboardTextActor3D',
                  'vtkCaptionActor2D', 'vtkCubeAxesActor')


def is_label_prop(prop) -> bool:
    """Whether *prop* draws text that tiling would leave at screen size."""
    if any(prop.IsA(name) for name in _LABEL_CLASSES):
        return True
    mapper = prop.GetMapper() if hasattr(prop, 'GetMapper') else None
    return bool(mapper is not None and mapper.IsA('vtkLabeledDataMapper'))


def label_props(renderer, *, visible_only: bool = True) -> list:
    """Every label prop of *renderer*."""
    found = []
    props = renderer.GetViewProps()
    props.InitTraversal()
    for index in range(props.GetNumberOfItems()):
        prop = props.GetItemAsObject(index)
        if visible_only and not prop.GetVisibility():
            continue
        if is_label_prop(prop):
            found.append(prop)
    return found


def text_properties(prop) -> list:
    """Every ``vtkTextProperty`` of one label prop."""
    found = []

    def add(item):
        if item is not None and all(item is not other for other in found):
            found.append(item)

    for name in ('GetTextProperty', 'GetLabelTextProperty',
                 'GetTitleTextProperty', 'GetAnnotationTextProperty',
                 'GetCaptionTextProperty'):
        getter = getattr(prop, name, None)
        if getter is None:
            continue
        try:
            add(getter())
        except TypeError:
            continue
    mapper = prop.GetMapper() if hasattr(prop, 'GetMapper') else None
    if mapper is not None and mapper.IsA('vtkLabeledDataMapper'):
        add(mapper.GetLabelTextProperty())
    if prop.IsA('vtkCubeAxesActor'):
        for axis in range(3):
            add(prop.GetLabelTextProperty(axis))
            add(prop.GetTitleTextProperty(axis))
    return found


class _Scaled:
    """Multiply the screen-size quantities of some label props, then put them back."""

    def __init__(self, props, factor: float):
        self._saved = []
        self._factor = float(factor)
        for prop in props:
            fonts = [(item, item.GetFontSize()) for item in text_properties(prop)]
            anchor = None
            if prop.IsA('vtkTextActor') and \
                    prop.GetPositionCoordinate().GetCoordinateSystem() in (0, 2):
                anchor = tuple(prop.GetPositionCoordinate().GetValue())
            self._saved.append((prop, fonts, anchor))

    def __enter__(self):
        for prop, fonts, anchor in self._saved:
            for item, size in fonts:
                item.SetFontSize(max(1, int(round(size * self._factor))))
            if anchor is not None:
                prop.GetPositionCoordinate().SetValue(
                    anchor[0] * self._factor, anchor[1] * self._factor)
        return self

    def __exit__(self, *_exc):
        for prop, fonts, anchor in self._saved:
            for item, size in fonts:
                item.SetFontSize(size)
            if anchor is not None:
                prop.GetPositionCoordinate().SetValue(*anchor)


def _read(window, scale: int = 1) -> np.ndarray:
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter

    grab = vtkWindowToImageFilter()
    grab.SetInput(window)
    grab.SetScale(int(scale))
    grab.ReadFrontBufferOff()
    grab.Update()
    data = grab.GetOutput()
    width, height, _depth = data.GetDimensions()
    array = vtk_to_numpy(data.GetPointData().GetScalars())
    return array.reshape(height, width, -1)[::-1, :, :3].copy()


def _label_layer(renderer, props, width: int, height: int, scale: int):
    """``(rgb, alpha)`` of *props* drawn at ``scale`` x, over nothing."""
    import vtkmodules.vtkRenderingOpenGL2      # noqa: F401
    from vtkmodules.vtkRenderingCore import vtkRenderer, vtkRenderWindow

    window = vtkRenderWindow()
    window.SetOffScreenRendering(1)
    window.SetMultiSamples(0)
    window.SetSize(width * scale, height * scale)
    layer = vtkRenderer()
    layer.SetActiveCamera(renderer.GetActiveCamera())
    window.AddRenderer(layer)
    moved = []
    try:
        with _Scaled(props, scale):
            for prop in props:
                renderer.RemoveViewProp(prop)
                layer.AddViewProp(prop)
                moved.append(prop)
            layer.SetBackground(0.0, 0.0, 0.0)
            window.Render()
            black = _read(window)
            layer.SetBackground(1.0, 1.0, 1.0)
            window.Render()
            white = _read(window)
    finally:
        for prop in moved:
            layer.RemoveViewProp(prop)
            renderer.AddViewProp(prop)
        window.RemoveRenderer(layer)
        window.Finalize()
    coverage = 1.0 - (white.astype(np.float32)
                      - black.astype(np.float32)).mean(axis=2) / 255.0
    coverage = np.clip(coverage, 0.0, 1.0)
    safe = np.where(coverage > 0.003, coverage, 1.0)[:, :, None]
    rgb = np.clip(black.astype(np.float32) / safe, 0, 255)
    return rgb, coverage


def capture(window, renderer, scale: int = 1, *, hide=()) -> np.ndarray:
    """The window as an ``(h, w, 3)`` uint8 array at ``scale`` x, labels sharp.

    *hide* are props left out of the picture altogether (an Include box that
    is off).  Everything is put back, visible and in place, before returning.
    """
    scale = int(scale)
    hidden = [prop for prop in hide if prop.GetVisibility()]
    labels = [prop for prop in label_props(renderer)
              if all(prop is not other for other in hidden)]
    for prop in hidden:
        prop.SetVisibility(False)
    try:
        if scale <= 1 or not labels:
            window.Render()
            return _read(window, scale)
        # The labels leave the renderer for the length of the capture; the
        # tiled geometry is drawn without them and they are laid over it.
        for prop in labels:
            prop.SetVisibility(False)
        try:
            window.Render()
            base = _read(window, scale)
        finally:
            for prop in labels:
                prop.SetVisibility(True)
        width, height = (int(v) for v in window.GetSize())
        rgb, coverage = _label_layer(renderer, labels, width, height, scale)
        if rgb.shape[:2] != base.shape[:2]:
            logger.warning('label layer %s does not match the picture %s',
                           rgb.shape[:2], base.shape[:2])
            return base
        blend = (base.astype(np.float32) * (1.0 - coverage[:, :, None])
                 + rgb * coverage[:, :, None])
        return np.clip(blend + 0.5, 0, 255).astype(np.uint8)
    finally:
        for prop in hidden:
            prop.SetVisibility(True)
        try:
            window.Render()
        except Exception:                                     # noqa: BLE001
            logger.debug('re-render after a capture failed', exc_info=True)


def write_array(rgb: np.ndarray, path, fmt: str) -> None:
    """Write an ``(h, w, 3)`` array as PNG or JPEG."""
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonDataModel import vtkImageData
    from vtkmodules.vtkIOImage import vtkJPEGWriter, vtkPNGWriter

    height, width = rgb.shape[:2]
    image = vtkImageData()
    image.SetDimensions(width, height, 1)
    flat = np.ascontiguousarray(rgb[::-1])
    scalars = numpy_to_vtk(flat.reshape(-1, 3), deep=True)
    scalars.SetName('RGB')
    image.GetPointData().SetScalars(scalars)
    writer = vtkPNGWriter() if fmt.upper() == 'PNG' else vtkJPEGWriter()
    writer.SetFileName(str(path))
    writer.SetInputData(image)
    writer.Write()
