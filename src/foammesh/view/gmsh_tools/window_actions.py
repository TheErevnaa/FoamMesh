"""What the window does for the Gmsh route's picture, movie and watch controls.

Plan 48 GM11b (DP-4410, DP-4055).  The window and the local API reach the
same code: this class is what the window registers on the case session
(:func:`foammesh.core.facade.plan48.gm13_routes.register_window_actions`), and
the File menu entries (Save as picture, Record movie, Watch pattern) call the
same methods, so a control a person has in the window is a route on the API
and the two cannot differ.

The viewport and the timers live on the GUI thread.  A call from another
thread (the API's) is handed over with :func:`on_gui_thread` and waited for.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from foammesh.core.facade.plan48.gm11b_routes import ensure_folder


def on_gui_thread(function, *args, timeout: float = 600.0):
    """Run ``function(*args)`` on the GUI thread; its value or its exception."""
    from PySide6.QtCore import QCoreApplication, QThread, QTimer

    application = QCoreApplication.instance()
    if application is None or QThread.currentThread() is application.thread():
        return function(*args)
    done = threading.Event()
    box: dict = {}

    def run():
        try:
            box['value'] = function(*args)
        except BaseException as error:                # noqa: BLE001
            box['error'] = error
        finally:
            done.set()

    QTimer.singleShot(0, application, run)
    if not done.wait(timeout):
        raise ValueError('the window did not answer in time')
    if 'error' in box:
        raise box['error']
    return box.get('value')


class WatchPattern:
    """File > Watch pattern: merge what matches when it appears or changes."""

    def __init__(self, merge, parent=None):
        from PySide6.QtCore import QTimer

        self._merge = merge
        self._timer = QTimer(parent)
        self._timer.timeout.connect(self.scan_once)
        self._pattern = ''
        self._interval = 0.0
        self._seen: dict | None = None
        self.merged: list = []
        self.refused: list = []
        self.last_error = ''

    def start(self, pattern, interval) -> dict:
        from foammesh.core.gmsh import import_formats as formats

        try:
            seconds = formats.check_watch_interval(interval)
            if not str(pattern).strip():
                raise formats.ImportRefusal(
                    'enter a file pattern such as results/*.pos')
        except formats.ImportRefusal as error:
            raise ValueError(str(error)) from error
        self._pattern = str(pattern)
        self._interval = seconds
        self._seen = None
        self.merged, self.refused, self.last_error = [], [], ''
        self._timer.start(int(seconds * 1000))
        self.scan_once()
        return self.status()

    def stop(self) -> dict:
        self._timer.stop()
        return self.status()

    def active(self) -> bool:
        return self._timer.isActive()

    def status(self) -> dict:
        return {'active': self.active(), 'pattern': self._pattern,
                'interval': self._interval, 'merged': list(self.merged),
                'refused': list(self.refused), 'error': self.last_error}

    def scan_once(self) -> list:
        """One scan; the files merged this time."""
        from foammesh.core.gmsh import import_formats as formats

        try:
            changed, self._seen = formats.watch_scan(self._pattern, self._seen)
        except formats.ImportRefusal as error:
            self.last_error = str(error)
            self._timer.stop()
            return []
        done = []
        for path, reason in changed:
            if reason:
                self.refused.append({'path': path, 'reason': reason})
                continue
            try:
                self._merge(path)
            except Exception as error:                # noqa: BLE001
                self.refused.append({'path': path, 'reason': str(error)})
                continue
            self.merged.append(path)
            done.append(path)
        return done


class WindowGmshActions:
    """The object the window registers for the API (see ``gm13_routes``)."""

    def __init__(self, view_provider, controller_provider, parent=None):
        self._view = view_provider
        self._controller = controller_provider
        self.watch = WatchPattern(self._merge, parent)

    # -- helpers ---------------------------------------------------------- #

    def _merge(self, path) -> dict:
        controller = self._controller()
        if controller is None:
            raise ValueError('open a project first')
        return controller.open_model(path, 'merge')

    def _the_view(self):
        view = self._view()
        if view is None:
            raise ValueError('the window has no viewport')
        return view

    # -- Save as picture --------------------------------------------------- #

    def save_image(self, path, format_id, options) -> dict:
        return on_gui_thread(self._save_image, path, format_id,
                             dict(options or {}))

    def _save_image(self, path, format_id, options) -> dict:
        from foammesh.rendering import image_formats as images

        try:
            item = images.format_for(format_id)
        except images.ImageFormatError as error:
            raise ValueError(str(error)) from error
        if item.kind == 'movie':
            raise ValueError('MP4 is a movie; use Record')
        reason = images.refusal(item.id)
        if reason:
            raise ValueError(reason)
        view = self._the_view()
        target = Path(path)
        ensure_folder(target)
        scale = int(options.get('scale') or 2)
        includes = {'transparent': bool(options.get('transparent'))}
        written = self._write(view, images, item, target, scale, includes)
        if not written or not Path(written).is_file():
            raise ValueError('the viewport could not draw the picture now; '
                             'try again when it is not busy')
        return {'path': str(written), 'format': item.id,
                'bytes': Path(written).stat().st_size}

    def _write(self, view, images, item, target, scale, includes):
        if item.id in ('PNG', 'JPEG', 'SVG', 'PDF', 'EPS', 'PS'):
            return view.saveImage(target.with_suffix(item.suffix), item.id,
                                  scale=scale, includes=includes)
        if item.id in images.RASTER_WRITERS:
            picture = view.grabImage(
                scale, transparent=bool(includes.get('transparent')))
            if picture is None:
                return None
            return images.write_raster(_buffer_of(picture), target, item.id)
        if item.id in images.VECTOR_TEXT:
            with tempfile.TemporaryDirectory() as folder:
                svg = view.saveImage(Path(folder) / 'view.svg', 'SVG',
                                     scale=scale, includes=includes)
                if not svg:
                    return None
                return images.write_vector_text(svg, target, item.id)
        if item.id == 'TEX':
            tex, _picture = images.write_tex(view.renderWindow(), target)
            return tex
        raise ValueError(f'{item.id} cannot be written from the window')

    # -- Record ------------------------------------------------------------ #

    def record_movie(self, path, options) -> dict:
        return on_gui_thread(self._record_movie, path, dict(options or {}))

    def _record_movie(self, path, options) -> dict:
        from foammesh.core import viewport_state
        from foammesh.rendering import image_formats as images

        reason = images.movie_refusal()
        if reason:
            raise ValueError(reason)
        view = self._the_view()
        frames = int(options.get('frames') or 60)
        fps = float(options.get('fps') or 25.0)
        quality = int(options.get('quality') if options.get('quality')
                      is not None else 18)
        try:
            steps = viewport_state.orbit_frames(frames)
        except Exception as error:                    # noqa: BLE001
            raise ValueError(str(error)) from error
        target = Path(path)
        ensure_folder(target)
        camera = view.renderer().GetActiveCamera()
        with tempfile.TemporaryDirectory() as folder:
            for index, step in enumerate(steps):
                camera.Azimuth(step.azimuth)
                view.renderer().ResetCameraClippingRange()
                name = Path(folder) / ('frame_%06d.png' % index)
                if not view.saveScreenshot(name, scale=1):
                    raise ValueError('the viewport could not draw frame %d'
                                     % index)
            try:
                written = images.encode_mp4(
                    str(Path(folder) / 'frame_%06d.png'), target, fps,
                    quality)
            except images.ImageFormatError as error:
                raise ValueError(str(error)) from error
        view.refresh()
        return {'path': str(written), 'frames': frames, 'fps': fps,
                'bytes': Path(written).stat().st_size}

    # -- Watch pattern ----------------------------------------------------- #

    def watch_start(self, pattern, interval) -> dict:
        return on_gui_thread(self.watch.start, pattern, interval)

    def watch_stop(self) -> dict:
        return on_gui_thread(self.watch.stop)

    def watch_status(self) -> dict:
        return self.watch.status()


def _buffer_of(image):
    """An (h, w, 4) uint8 array from a ``QImage``."""
    import numpy as np
    from PySide6.QtGui import QImage

    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    pointer = converted.constBits()
    array = np.frombuffer(pointer, dtype=np.uint8,
                          count=converted.sizeInBytes())
    stride = converted.bytesPerLine()
    return array.reshape(height, stride)[:, :width * 4].reshape(
        height, width, 4).copy()
