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


#: Captures under way. While one is, a label layout that follows the camera
#: (``worst_labels.WorstLabelLayer.relayout``) keeps the places it had on the
#: screen: the tiles of a magnified capture move the camera tile by tile.
_capturing = 0


def capturing() -> bool:
    """Whether a magnified capture is drawing now (layouts must hold still)."""
    return _capturing > 0


def is_overlay_shape(prop) -> bool:
    """DP-2740. A plain 2-D polydata in display pixels: a label's box, frame,
    leader or anchor dot (``worst_labels._Shapes``).

    Tiling magnifies the 3-D scene but not these, so a 2x snapshot drew the
    worst-value boxes at screen size in the wrong tile -- blank black bars in
    the middle of the model -- with their text elsewhere. They are drawn with
    the labels, at the picture's size.
    """
    try:
        if prop.GetClassName() != 'vtkActor2D':
            return False
        mapper = prop.GetMapper()
        return bool(mapper is not None and mapper.IsA('vtkPolyDataMapper2D')
                    and mapper.GetTransformCoordinate() is None
                    and prop.GetPositionCoordinate().GetCoordinateSystem()
                    in (0, 2))
    except Exception:                                         # noqa: BLE001
        return False


def _scaled_copy(data, factor: float):
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkCommonCore import vtkPoints
    copy = vtkPolyData()
    copy.ShallowCopy(data)
    points = data.GetPoints()
    if points is not None:
        moved = vtkPoints()
        moved.SetDataTypeToDouble()
        moved.SetNumberOfPoints(points.GetNumberOfPoints())
        for index in range(points.GetNumberOfPoints()):
            x, y, z = points.GetPoint(index)
            moved.SetPoint(index, x * factor, y * factor, z)
        copy.SetPoints(moved)
    return copy


class _Scaled:
    """Multiply the screen-size quantities of some label props, then put them back."""

    def __init__(self, props, factor: float):
        self._saved = []
        self._shapes = []
        self._factor = float(factor)
        for prop in props:
            if is_overlay_shape(prop):
                mapper = prop.GetMapper()
                self._shapes.append(
                    (prop, mapper, mapper.GetInput(),
                     tuple(prop.GetPositionCoordinate().GetValue()),
                     prop.GetProperty().GetLineWidth(),
                     prop.GetProperty().GetPointSize()))
                continue
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
        for prop, mapper, data, anchor, width, point in self._shapes:
            if data is not None:
                mapper.SetInputData(_scaled_copy(data, self._factor))
            prop.GetPositionCoordinate().SetValue(
                anchor[0] * self._factor, anchor[1] * self._factor)
            prop.GetProperty().SetLineWidth(width * self._factor)
            prop.GetProperty().SetPointSize(point * self._factor)
        return self

    def __exit__(self, *_exc):
        for prop, fonts, anchor in self._saved:
            for item, size in fonts:
                item.SetFontSize(size)
            if anchor is not None:
                prop.GetPositionCoordinate().SetValue(*anchor)
        for prop, mapper, data, anchor, width, point in self._shapes:
            if data is not None:
                mapper.SetInputData(data)
            prop.GetPositionCoordinate().SetValue(*anchor)
            prop.GetProperty().SetLineWidth(width)
            prop.GetProperty().SetPointSize(point)


def _read(window, scale: int = 1, *, rgba: bool = False) -> np.ndarray:
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter

    grab = vtkWindowToImageFilter()
    grab.SetInput(window)
    grab.SetScale(int(scale))
    grab.ReadFrontBufferOff()
    if rgba:
        grab.SetInputBufferTypeToRGBA()
    grab.Update()
    data = grab.GetOutput()
    width, height, _depth = data.GetDimensions()
    array = vtk_to_numpy(data.GetPointData().GetScalars())
    channels = 4 if rgba else 3
    return array.reshape(height, width, -1)[::-1, :, :channels].copy()


def _label_layer(renderer, props, width: int, height: int, scale: int):
    """``(rgb, alpha, box)`` of *props* drawn at ``scale`` x, over nothing.

    DP-2741. One render over a transparent ground, read as RGBA: the alpha
    (uint8) is the labels' coverage and the colour, drawn over black, is
    already premultiplied by it, so laying it over a picture is
    ``under * (1 - alpha) + rgb``. It used to be two renders and two reads
    (over black, over white) and float arithmetic on every pixel of the
    picture -- 0.6 s of a 1.3 s hold for a 2x snapshot of a 1920 x 872 view.
    The pixels returned are *box* only, ``(y0, y1, x0, x1)``, the rows and
    columns the labels cover; ``None`` when they cover nothing.
    """
    import vtkmodules.vtkRenderingOpenGL2      # noqa: F401
    from vtkmodules.vtkRenderingCore import vtkRenderer, vtkRenderWindow

    window = vtkRenderWindow()
    window.SetOffScreenRendering(1)
    window.SetMultiSamples(0)
    window.SetAlphaBitPlanes(1)
    window.SetSize(width * scale, height * scale)
    layer = vtkRenderer()
    layer.SetActiveCamera(renderer.GetActiveCamera())
    layer.SetBackground(0.0, 0.0, 0.0)
    layer.SetBackgroundAlpha(0.0)
    window.AddRenderer(layer)
    moved = []
    try:
        with _Scaled(props, scale):
            for prop in props:
                renderer.RemoveViewProp(prop)
                layer.AddViewProp(prop)
                moved.append(prop)
            window.Render()
            image = _read(window, rgba=True)
    finally:
        for prop in moved:
            layer.RemoveViewProp(prop)
            renderer.AddViewProp(prop)
        window.RemoveRenderer(layer)
        window.Finalize()
    alpha = image[:, :, 3]
    rows = np.flatnonzero(alpha.any(axis=1))
    if rows.size == 0:
        return None, None, None
    cols = np.flatnonzero(alpha.any(axis=0))
    box = (int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1)
    y0, y1, x0, x1 = box
    return (image[y0:y1, x0:x1, :3], image[y0:y1, x0:x1, 3],
            (box, image.shape[:2]))


def _overlay_props(renderer, hidden) -> list:
    """Visible labels and label shapes, in the renderer's drawing order."""
    found = []
    props = renderer.GetViewProps()
    for index in range(props.GetNumberOfItems()):
        prop = props.GetItemAsObject(index)
        if not prop.GetVisibility() or any(prop is other for other in hidden):
            continue
        if is_label_prop(prop) or is_overlay_shape(prop):
            found.append(prop)
    return found


def capture(window, renderer, scale: int = 1, *, hide=()) -> np.ndarray:
    """The window as an ``(h, w, 3)`` uint8 array at ``scale`` x, labels sharp.

    *hide* are props left out of the picture altogether (an Include box that
    is off).  Everything is put back, visible and in place, before returning.
    """
    global _capturing
    scale = int(scale)
    hidden = [prop for prop in hide if prop.GetVisibility()]
    for prop in hidden:
        prop.SetVisibility(False)
    try:
        if scale > 1:
            # DP-2740: lay the labels out once for the camera on the screen;
            # from here until the capture ends they hold still (the tiles
            # move the camera).
            window.Render()
        labels = _overlay_props(renderer, hidden) if scale > 1 else []
        if scale <= 1 or not labels:
            window.Render()
            return _read(window, scale)
        # The labels leave the renderer for the length of the capture; the
        # tiled geometry is drawn without them and they are laid over it.
        _capturing += 1
        for prop in labels:
            prop.SetVisibility(False)
        try:
            # The tiling draws every tile itself: no render first.
            base = _read(window, scale)
        finally:
            for prop in labels:
                prop.SetVisibility(True)
            _capturing -= 1
        width, height = (int(v) for v in window.GetSize())
        rgb, coverage, where = _label_layer(renderer, labels, width, height,
                                            scale)
        if rgb is None:
            return base
        (y0, y1, x0, x1), shape = where
        if tuple(shape) != tuple(base.shape[:2]):
            logger.warning('label layer %s does not match the picture %s',
                           shape, base.shape[:2])
            return base
        # Premultiplied "over", in integers.
        keep = (255 - coverage.astype(np.uint16))[:, :, None]
        under = base[y0:y1, x0:x1].astype(np.uint16)
        blend = (under * keep + 127) // 255 + rgb
        base[y0:y1, x0:x1] = np.minimum(blend, 255).astype(np.uint8)
        return base
    finally:
        for prop in hidden:
            prop.SetVisibility(True)
        try:
            window.Render()
        except Exception:                                     # noqa: BLE001
            logger.debug('re-render after a capture failed', exc_info=True)


#: The PNG writes held back by :func:`deferred_writes`, or None.
_deferred: list | None = None


class deferred_writes:
    """Hold back the PNG writes made inside the block (DP-2741).

    Encoding a 2x snapshot (3840 x 1744) took 0.22 s of the 0.71 s the GUI
    stood still on Save.  Inside the block :func:`write_array` copies the
    pixels, reserves the file name with an empty file (so the next numbered
    name moves on) and queues the write; :meth:`start` then encodes and
    writes them on a worker thread -- numpy and zlib, no VTK and no Qt -- and
    swaps each file in whole.  The Save button and the API's snapshot use
    it; the API answers once the file is down (``presentation_bridge``'s
    ``settle``, awaited by the facade).
    """

    def __init__(self):
        self.jobs: list = []
        self._outer = None

    def __enter__(self):
        global _deferred
        self._outer, _deferred = _deferred, self.jobs
        return self

    def __exit__(self, *exc):
        global _deferred
        _deferred = self._outer
        return False

    def start(self):
        """Write the queued files on a worker thread; returns the thread, or
        None when nothing was queued."""
        if not self.jobs:
            return None
        import threading

        jobs, self.jobs = list(self.jobs), []
        thread = threading.Thread(target=_write_jobs, args=(jobs,),
                                  name='snapshot-png', daemon=False)
        thread.start()
        return thread


def _write_jobs(jobs) -> None:
    import os
    from pathlib import Path

    for rgb, path in jobs:
        path = Path(path)
        part = path.with_name(path.name + '.part')
        try:
            part.write_bytes(encode_png(rgb))
            os.replace(part, path)
        except Exception:                                     # noqa: BLE001
            logger.exception('writing %s failed', path)
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass


def encode_png(rgb: np.ndarray, level: int = 6) -> bytes:
    """An ``(h, w, 3)`` uint8 array as PNG bytes (Sub filter, zlib).

    Plain numpy and zlib, so it can run off the GUI thread (zlib lets go of
    the GIL while it compresses).  Read back by VTK's PNG reader it is the
    same array.
    """
    import struct
    import zlib

    rgb = np.ascontiguousarray(rgb[..., :3], dtype=np.uint8)
    height, width = rgb.shape[:2]
    rows = rgb.reshape(height, width * 3)
    raw = np.empty((height, width * 3 + 1), np.uint8)
    raw[:, 0] = 1                                   # filter type 1: Sub
    raw[:, 1:4] = rows[:, :3]
    np.subtract(rows[:, 3:], rows[:, :-3], out=raw[:, 4:], dtype=np.uint8,
                casting='unsafe')

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack('>I', len(body)) + kind + body
                + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff))

    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    signature = bytes((137, 80, 78, 71, 13, 10, 26, 10))
    return (signature + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(raw.tobytes(), level))
            + chunk(b'IEND', b''))


def write_array(rgb: np.ndarray, path, fmt: str) -> None:
    """Write an ``(h, w, 3)`` array as PNG or JPEG (a PNG is queued instead
    inside :class:`deferred_writes`)."""
    if _deferred is not None and fmt.upper() == 'PNG':
        from pathlib import Path

        Path(path).write_bytes(b'')                 # the name is taken
        _deferred.append((np.array(rgb[..., :3], dtype=np.uint8), path))
        return
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
