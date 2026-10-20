#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Take a picture of the mesh, and record what the picture is of.

The fast path writes into the case with no dialog at all. What makes the
capture worth keeping is the sidecar written beside it: the camera, what was
visible, the active section and the mesh fingerprint. That is what lets the
gallery put the view back, and what lets a picture of a superseded mesh be
labelled instead of mistaken for the current one.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from foammesh.app import app
from foammesh.view.facade_client import NoOpenCaseError
from foammesh.core import snapshot as snapshot_core
from foammesh.view.widget_factory import compose
from foammesh.core.capture import (
    CaptureRecord, capture_paths, captures_dir, ensure_captures_dir,
    list_captures,
    new_stamp, sidecar_for, write_record)


logger = logging.getLogger(__name__)


class CaptureManager(QObject):
    captured = Signal(str)
    #: What the last ``snapshot`` said: the line for the status bar, or why it
    #: wrote nothing.
    snapshotMessage = ''

    def __init__(self, view, displayControl):
        super().__init__()
        self._view = view
        self._displayControl = displayControl

    def caseRoot(self) -> Path | None:
        """Where this case's files live, or ``None`` when no case is open.

        DP-31. The three-argument `getattr` below reads as a guard against
        there being no case and is not one: its default answers an absent
        attribute, and `case_root` is a property that is present and *raises*
        `NoOpenCaseError` when the session has gone. Closing a case while the
        viewport was still rebuilding its overlay -- which is what the end of
        every meshing leg does -- therefore reached Qt's handler as an uncaught
        exception. The refresh has no case to report on at that point, which is
        not an error; it is the answer.
        """
        client = getattr(app, 'facadeClient', None)
        try:
            root = getattr(client, 'case_root', None)
        except NoOpenCaseError:
            return None
        return Path(root) if root else None

    def capture(self, *, scale: int = 2, label: str = '',
                stamp: str | None = None) -> Path | None:
        """Write a PNG and its sidecar into the case. Returns the image path."""
        root = self.caseRoot()
        if root is None:
            return None

        ensure_captures_dir(root)
        image, sidecar = capture_paths(root, stamp or new_stamp())
        if self._view.saveScreenshot(str(image), scale=scale) is False:
            # Plan 35 CR8: the view is not drawing (safe mode, a driver
            # reset, a render hold); no image, so no record of one.
            return None
        write_record(sidecar, self.buildRecord(image.name, label))
        self.captured.emit(str(image))
        return image

    def suggestedName(self, fmt: str = 'PNG', directory=None) -> str:
        """The next numbered name, for the popover's File name placeholder."""
        root = self.caseRoot()
        folder = Path(directory) if directory else (
            captures_dir(root) if root is not None else Path('.'))
        return snapshot_core.numbered_name(
            folder, snapshot_core.suggest_stem(self._caseName(), self._scalar()),
            fmt)

    def snapshot(self, options) -> Path | None:
        """Write the picture the snapshot popover describes, and its sidecar.

        One writer for the three things that used to be separate: the numbered
        capture in the case, the saved view and the saved image.  *options* is
        a :class:`foammesh.core.snapshot.SnapshotOptions`.  Returns the file
        written, or None when there was nowhere to write it or the view cannot
        draw (safe mode, a render hold); ``snapshotMessage`` says what happened.
        """
        opts = options.normalised()
        self.snapshotMessage = ''
        if opts.directory:
            directory, where = Path(opts.directory), Path(opts.directory).name
        else:
            root = self.caseRoot()
            if root is None:
                self.snapshotMessage = self.tr(
                    'Open a case before taking a snapshot, or choose a folder.')
                return None
            directory = ensure_captures_dir(root)
            where = self.tr('case › captures')
        suffix = snapshot_core.SUFFIX[opts.format]
        stem = Path(opts.name).stem if opts.name else ''
        if stem:
            path = snapshot_core.unique_path(directory, stem + suffix)
        else:
            path = directory / snapshot_core.numbered_name(
                directory, snapshot_core.suggest_stem(
                    self._caseName(), self._scalar()), opts.format)
        view = self._view
        wanted = (opts.font, opts.text_size)
        style = view.labelStyle()
        if (style.font, style.size) != wanted:
            view.setLabelStyle(font=opts.font, size=opts.text_size)
        includes = dict(opts.includes)
        written = self._writeSnapshot(path, opts, includes)
        if written is None:
            self.snapshotMessage = self.tr(
                'The viewport is not drawing, so there is nothing to save.')
            return None
        size = self._pixelSize(opts)
        record = self.buildRecord(Path(written).name)
        from dataclasses import replace

        record = replace(record, **snapshot_core.sidecar_fields(opts, size))
        write_record(sidecar_for(written), record)
        self.snapshotMessage = snapshot_core.status_line(
            written, opts, size, where)
        self.captured.emit(str(written))
        return Path(written)

    def _writeSnapshot(self, path: Path, opts, includes):
        with self.labelsFor(includes), self.pictureCaption(includes):
            return self._writeSnapshotNow(path, opts, includes)

    def _labelLayer(self):
        views = getattr(getattr(app, 'window', None), '_qualityViews', None)
        return getattr(views, '_layer', None)

    def labelsFor(self, includes):
        """The worst values and the statistics caption the Include boxes
        leave out, held off for one capture (DP-2770).

        Both boxes switched nothing: the view hides the props registered in
        its 'worst' and 'caption' label groups, and the worst-value layer
        registered none. The layer now follows the view's switches on
        screen, and a snapshot whose boxes are unticked (the API's, or the
        popover's) leaves the groups out of the picture.
        """
        import contextlib

        layer = self._labelLayer()
        off = [group for group in ('worst', 'caption')
               if (includes or {}).get(group) is False]
        if layer is None or not off or not hasattr(layer, 'suppressed'):
            return contextlib.nullcontext()
        return layer.suppressed(off)

    def captionText(self) -> str:
        """The statistics the legend shows, one per line, for the picture.

        DP-2740. The snapshot's *Caption* box was ticked and the picture had
        no statistics: nothing drew a caption (the Qt legend that shows the
        figures on screen is not in the render window). '' when the model
        is not coloured by a scalar, so a legend left over from an earlier
        colouring is never captioned.
        """
        # Deviation only: Quality's statistics caption is the worst-value
        # layer's own, on screen and in the picture alike (Plan 45 P2 took
        # Deviation's off the screen; the picture has no Qt legend).
        if self._scalar() != 'deviation':
            return ''
        controller = getattr(getattr(app, 'window', None),
                             '_legendController', None)
        legend = getattr(controller, 'legend', None)
        spec = legend.spec() if legend is not None else None
        lines = [str(line) for line in getattr(spec, 'statistics', ()) or ()]
        if not lines:
            return ''
        title = str(getattr(spec, 'title', '') or '')
        return '\n'.join([title, *lines] if title else lines)

    def pictureCaption(self, includes):
        """The caption in the render window for the length of one capture.

        A text actor, top left, white on a dark plate like the worst-value
        labels; :mod:`foammesh.rendering.label_pass` draws it at the
        picture's size. Off the screen before and after.
        """
        import contextlib

        text = (self.captionText()
                if (includes or {}).get('caption', True) is not False else '')
        renderer = getattr(self._view, 'renderer', None)
        renderer = renderer() if callable(renderer) else None
        if not text or renderer is None:
            return contextlib.nullcontext()
        from vtkmodules.vtkRenderingCore import vtkTextActor

        actor = vtkTextActor()
        actor.SetObjectName('snapshotCaption')
        actor.SetInput(text)
        coordinate = actor.GetPositionCoordinate()
        coordinate.SetCoordinateSystemToNormalizedViewport()
        coordinate.SetValue(0.012, 0.975)
        style = actor.GetTextProperty()
        style.SetFontSize(13)
        style.SetJustificationToLeft()
        style.SetVerticalJustificationToTop()
        style.SetColor(1.0, 1.0, 1.0)
        style.SetBackgroundColor(0.12, 0.13, 0.15)
        style.SetBackgroundOpacity(0.82)
        style.SetLineSpacing(1.15)
        style.SetFrame(False)

        @contextlib.contextmanager
        def shown():
            renderer.AddViewProp(actor)
            try:
                yield actor
            finally:
                renderer.RemoveViewProp(actor)

        return shown()

    def _writeSnapshotNow(self, path: Path, opts, includes):
        from .view_capture import composeView

        raster = snapshot_core.is_raster(opts.format)
        if raster and (includes.get('transparent') or opts.overlay):
            image = compose(
                composeView, self._view, scale=opts.scale,
                transparent=bool(includes.get('transparent')),
                legends=bool(includes.get('legend', True)),
                overlay=bool(opts.overlay), includes=includes)
            if image is None:
                return None
            return path if image.save(str(path), opts.format) else None
        return self._view.saveImage(path, opts.format, scale=opts.scale,
                                    includes=includes)

    def _pixelSize(self, opts) -> tuple:
        try:
            width, height = (int(v) for v in
                             self._view.renderWindow().GetSize())
        except Exception:                                     # noqa: BLE001
            width, height = self._view.width(), self._view.height()
        return snapshot_core.pixel_size(width, height, opts.scale)

    def _caseName(self) -> str:
        root = self.caseRoot()
        return root.name if root is not None else ''

    def buildRecord(self, imageName: str, label: str = '') -> CaptureRecord:
        from datetime import datetime, timezone

        return CaptureRecord(
            image=imageName,
            created=datetime.now(timezone.utc).isoformat(),
            label=label,
            camera=self._camera(),
            visible_actors=self._visibleActors(),
            section=self._section(),
            scalar=self._scalar(),
            scalar_band=self._scalarBand(),
            mesh_fingerprint=self.meshFingerprint(),
            run_id=self._meshValue('runId'),
            artifact_id=self._meshValue('artifactId'),
            result=self._meshValue('resultLabel'),
        )

    def restore(self, record: CaptureRecord) -> bool:
        """Put the camera and the visible set back the way the picture had them."""
        if not record.camera:
            return False
        self._view.rememberView()
        self._view.restoreCameraState(record.camera)
        if record.visible_actors:
            self._displayControl.isolate(record.visible_actors)
        return True

    def captures(self):
        root = self.caseRoot()
        return list_captures(root) if root is not None else []

    # -- WP8.3: canned sequences, written as frames ------------------------ #

    def encoderAvailable(self) -> tuple[bool, str]:
        """Whether anything on this machine can turn frames into a video.

        Capability-gated like every other external tool: if no encoder is
        discovered the app says so up front and leaves the frames, rather than
        failing at the end of a long render.
        """
        capabilities = getattr(app, 'capabilities', None)
        if capabilities is None:
            return False, self.tr('No encoder was looked for')
        for name in ('ffmpeg', 'avconv'):
            try:
                utility = capabilities.utility(name)
            except Exception:
                continue
            if getattr(utility, 'available', False):
                return True, name
        return False, self.tr(
            'No video encoder found. The frames are kept; encode them with '
            'ffmpeg when you have one.')

    def renderOrbit(self, frames=None, *, label='orbit') -> list[Path]:
        """A full revolution about the up axis, one PNG per frame."""
        from foammesh.core.viewport_state import frame_name, orbit_frames

        plan = frames if frames is not None else orbit_frames()
        camera = self._view.renderer().GetActiveCamera()
        restore = self._view.cameraState()
        written = []
        try:
            for frame in plan:
                camera.Azimuth(frame.azimuth)
                self._view.refresh()
                path = self.capture(
                    scale=1, label=label,
                    stamp=frame_name(label, frame.index, len(plan)))
                if path is not None:
                    written.append(path)
        finally:
            self._view.restoreCameraState(restore)
        return written

    def renderSectionSweep(self, cutTool, frames=None,
                           *, label='sweep') -> list[Path]:
        """March the active section plane through the model, one PNG per frame.

        Uses the section the user already set up rather than inventing one, so
        the sweep answers the question they were already asking.
        """
        from foammesh.core.viewport_state import frame_name, sweep_frames

        panel = cutTool.panel()
        index = panel.activeIndex()
        plane = panel.planes()[index]
        bounds = getattr(cutTool, '_bounds', None)
        if bounds is None:
            return []

        original = list(plane.origin)
        wasEnabled = plane.enabled
        plan = (frames if frames is not None
                else sweep_frames(bounds, plane.normalised()))
        written = []
        try:
            if not wasEnabled:
                panel._planeToggled(index, True)
            for frame in plan:
                panel.setPlaneGeometry(index, frame.origin, plane.normal)
                cutTool._apply()
                path = self.capture(
                    scale=1, label=label,
                    stamp=frame_name(label, frame.index, len(plan)))
                if path is not None:
                    written.append(path)
        finally:
            panel.setPlaneGeometry(index, original, plane.normal)
            if not wasEnabled:
                panel._planeToggled(index, False)
            cutTool._apply()
        return written

    def _meshValue(self, name: str) -> str:
        """Ask the mesh manager one question, tolerating there being none.

        The manager is created and destroyed with the project, and a capture
        can be taken from an empty viewport.
        """
        manager = getattr(app.window, 'meshManager', None)
        getter = getattr(manager, name, None)
        if getter is None:
            return ''
        try:
            return str(getter() or '')
        except Exception:                                     # noqa: BLE001
            logger.debug('could not read %s for a capture', name, exc_info=True)
            return ''

    def meshFingerprint(self) -> str:
        """The digest of the loaded polyMesh, or '' when there is nothing to sign.

        A capture with no fingerprint is never reported as stale -- claiming
        staleness without evidence is as misleading as hiding it.
        """
        # CP-09 item 8. A native Gmsh run has no `constant/polyMesh` at all
        # -- MEASURED: every refused candidate in the tier-1 sweep -- so the
        # polyMesh-only route signed nothing, and every Gmsh capture was
        # unstaleable by construction. The file the viewport is actually
        # drawing is the honest thing to sign.
        native = self._nativeFingerprint()
        if native:
            return native
        root = self.caseRoot()
        if root is None:
            return ''
        polyMesh = root / 'constant' / 'polyMesh'
        if not polyMesh.is_dir():
            return ''
        try:
            from foammesh.core.case import fingerprint_poly_mesh

            return fingerprint_poly_mesh(polyMesh).digest
        except Exception:
            logger.debug('could not fingerprint the mesh for a capture',
                         exc_info=True)
            return ''

    def _nativeFingerprint(self) -> str:
        """Sign the .msh on screen: its size, its mtime and its content hash.

        Content rather than path, because a re-run writes the same path.
        """
        manager = getattr(app.window, 'meshManager', None)
        getter = getattr(manager, 'nativePath', None)
        path = getter() if callable(getter) else None
        if path is None:
            return ''
        try:
            import hashlib

            digest = hashlib.sha256()
            with open(path, 'rb') as handle:
                for block in iter(lambda: handle.read(1 << 20), b''):
                    digest.update(block)
            return f'msh:{digest.hexdigest()}'
        except OSError:
            logger.debug('could not fingerprint the native mesh for a capture',
                         exc_info=True)
            return ''

    # -- what the picture was of ------------------------------------------- #

    def _camera(self):
        try:
            return self._view.cameraState()
        except Exception:
            return {}

    def _visibleActors(self):
        items = getattr(self._displayControl, '_items', {})
        return sorted(key for key, item in items.items()
                      if item.actorInfo().isVisible())

    def _section(self):
        tool = getattr(self._displayControl, 'cutTool', None)
        if tool is None:
            return {}
        panel = tool().panel()
        return {
            'type': panel.cutType().name,
            'crinkle': panel.isCrinkle(),
            'planes': [
                {'origin': list(plane.origin), 'normal': list(plane.normalised())}
                for plane in panel.enabledPlanes()
            ],
        }

    def _scalar(self):
        """What the model on screen is coloured by: ``deviation``, the quality
        metric being highlighted, or '' for the plain patch colours.

        DP-2740. This read the quality picker's metric whatever was on screen,
        so a snapshot of the deviation colouring was filed as
        ``dev_sweep_nonorthoangle_...``; a plain view was named after a metric
        it did not show either.
        """
        manager = getattr(getattr(app, 'window', None), 'meshManager', None)
        manager = manager() if callable(manager) else manager
        readout = getattr(manager, 'deviationReadout', None)
        try:
            if readout is not None and readout() is not None:
                return 'deviation'
        except Exception:                                     # noqa: BLE001
            logger.debug('no deviation readout for a capture', exc_info=True)
        info = getattr(self._displayControl, 'meshQualityInfo', None)
        if info is None:
            return ''
        quality = info()
        highlighting = getattr(quality, 'isHighlighting', None)
        if callable(highlighting) and not highlighting():
            return ''
        index = quality.activeIndex()
        return index.value if index is not None else ''

    def _scalarBand(self):
        info = getattr(self._displayControl, 'meshQualityInfo', None)
        return list(info().band()) if info is not None else []
