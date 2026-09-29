#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The viewport's mesh loader: the picture is built in a worker (Plan 35 CR3).

``PolyMeshLoader`` ran ``vtkPOpenFOAMReader`` in the window's own process and
asked it for the whole volume, so a mesh of a few million cells put gigabytes
in the process that must not die. This loader keeps its interface -- a
``progress`` signal, ``hasMesh``, ``await loadMesh(time)`` returning the
region -> boundary/internalMesh/zones scene, ``dispose`` -- and gets the scene
from :mod:`foammesh.core.mesh.mesh_preview` run in a worker instead:

1. the mesh's header counts are read (no list is parsed) and the preview's
   peak memory estimated from them;
2. the admission controller grants the one heavy worker slot, preview first
   (``PRIORITY_PREVIEW``), or refuses with both numbers;
3. the worker writes the boundary surface -- and below
   :data:`~foammesh.core.mesh.mesh_preview.VOLUME_AUTO_MAX_CELLS` cells, the
   volume -- into ``<case>/foammesh/cache/``, under a Job Object cap;
4. the sizes of those files are checked before any is opened, and then they
   are read on the VTK thread and deleted.

When the preview cannot be built -- refused, over its cap, failed -- the scene
is empty and :attr:`outline` / :attr:`notice` say what there is instead: the
mesh's bounding box, its patches and their face counts, and why. The mesh
itself is untouched: it stays done, exportable and checkable.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from foammesh.core.mesh import mesh_preview
from foammesh.support import disposal
from foammesh.support import resource_budget as budget
from foammesh.support.vtk_threads import vtk_run_in_thread

logger = logging.getLogger(__name__)

AUTO = 'auto'
SURFACE = 'surface'
VOLUME = 'volume'
#: "Build anyway" raises the budget to this share of physical memory.
BUILD_ANYWAY_SHARE = 0.9


def _outline_polydata(bounds):
    from vtkmodules.vtkFiltersSources import vtkOutlineSource

    source = vtkOutlineSource()
    source.SetBounds(*bounds)
    source.Update()
    return source.GetOutput()


class MeshPreviewLoader(QObject):
    progress = Signal(str)

    def __init__(self, foamFile, regionSeeds=(), *, mode: str = AUTO,
                 buildAnyway: bool = False):
        super().__init__()
        self._caseDir = Path(foamFile).parent
        self._regionSeeds = [[name, list(point)] for name, point in regionSeeds]
        self._mode = mode
        self._buildAnyway = bool(buildAnyway)
        #: Header counts of the mesh, or None when they could not be read.
        self.counts: dict | None = None
        #: ``actor id -> faces`` as the mesh has them, before any decimation.
        self.patchFaces: dict[str, int] = {}
        #: ``(region, category, name) -> {'edges', 'surface'}`` (step 10).
        self.precomputed: dict = {}
        #: The bounding box drawn in place of a preview that was not built.
        self.outline = None
        #: What to tell the user about the picture, or None: ``{'kind',
        #: 'text', 'patches', 'actions'}``.
        self.notice: dict | None = None
        #: What was drawn: 'surface', 'volume', 'outline' or ''.
        self.shown = ''
        disposal.track(self, 'MeshPreviewLoader')

    def dispose(self):
        if self._caseDir is None:
            return
        self._caseDir = None
        self.precomputed = {}
        disposal.untrack(self, 'MeshPreviewLoader')

    def hasMesh(self) -> bool:
        return mesh_preview.case_layout(self._caseDir) is not None

    # ------------------------------------------------------------------ #

    async def loadMesh(self, time):
        case = self._caseDir
        layout = await asyncio.to_thread(mesh_preview.case_layout, case)
        if layout is None:
            return None
        self.progress.emit(self.tr('Measuring the mesh…'))
        counts = await asyncio.to_thread(mesh_preview.case_counts, case, layout)
        if counts is not None:
            counts['bounded'] = await asyncio.to_thread(
                mesh_preview.bounded_readable, case, time, counts)
        self.counts = counts
        cells = mesh_preview.estimated_cells(counts)
        mode = self._mode
        if mode == AUTO:
            mode = (VOLUME if cells is not None
                    and cells <= mesh_preview.VOLUME_AUTO_MAX_CELLS
                    else SURFACE)
        result = await self._build(mode, time, counts)
        if result is _RETRY_AS_SURFACE:
            result = await self._build(SURFACE, time, counts)
        if result is _RETRY_AS_SURFACE:
            result = await self._outline(
                'preview_failed',
                self.tr('Preview not built: the worker refused the surface.'))
        if isinstance(result, dict) and self.shown == SURFACE and \
                self.notice is None and cells is not None and \
                cells > mesh_preview.VOLUME_AUTO_MAX_CELLS:
            self.notice = {
                'kind': 'surface',
                'text': self.tr('Showing the boundary surface of a mesh of '
                                '{0:,} cells.').format(cells),
                'patches': [], 'actions': ['load_volume']}
        return result

    async def _build(self, mode: str, time, counts):
        volume = mode == VOLUME
        operation = (mesh_preview.VOLUME_OPERATION if volume
                     else mesh_preview.OPERATION)
        estimate = budget.estimate_peak_bytes(operation, counts)
        override = None
        if self._buildAnyway:
            total, _available = await asyncio.to_thread(budget.physical_memory)
            override = int(total * BUILD_ANYWAY_SHARE)
        self.progress.emit(self.tr('Waiting for the mesh worker…'))
        try:
            grant = await budget.controller().admit(
                operation, estimate, priority=budget.PRIORITY_PREVIEW,
                budget_override=override)
        except budget.OverBudget as refusal:
            if self._mode == AUTO and volume:
                return _RETRY_AS_SURFACE
            return await self._outline(
                'over_budget',
                self.tr('Preview not built: it needs about {0} and {1} is '
                        'free.').format(
                    budget.format_bytes(refusal.estimate.peak_bytes),
                    budget.format_bytes(refusal.snapshot.budget)),
                refusal=True)
        from foammesh.core.jobs import local_worker

        max_bytes = (budget.FULL_VOLUME_MAX_BYTES if volume
                     else budget.PREVIEW_MAX_BYTES)
        args = {'case': str(self._caseDir), 'time': time,
                'region_seeds': self._regionSeeds,
                'out_dir': str(mesh_preview.cache_dir(self._caseDir)),
                'max_triangles': budget.PREVIEW_MAX_TRIANGLES,
                'max_bytes': max_bytes,
                'max_cells': budget.FULL_VOLUME_MAX_CELLS}
        self.progress.emit(self.tr('Building the mesh preview…'))
        async with grant:
            outcome = await local_worker.run_worker(
                operation, args, cap_bytes=grant.cap_bytes,
                group=str(self._caseDir))
        if not outcome.ok:
            logger.info('mesh preview %s: %s (%s)', outcome.status,
                        outcome.reason, outcome.message)
            if self._mode == AUTO and volume:
                return _RETRY_AS_SURFACE
            if outcome.status == local_worker.OVER_BUDGET:
                text = self.tr('Preview not built: the worker ran out of the '
                               '{0} it was given.').format(
                    budget.format_bytes(outcome.cap_bytes or grant.cap_bytes))
                return await self._outline('over_budget', text, refusal=True)
            if outcome.reason in ('preview_too_large', 'volume_too_large'):
                return await self._outline(
                    outcome.reason,
                    self.tr('Preview not built: {0}.').format(outcome.message),
                    refusal=volume is False)
            return await self._outline(
                outcome.reason or outcome.status,
                self.tr('Preview not built: {0}').format(
                    outcome.message or outcome.status))
        payload = outcome.payload or {}
        if payload.get('empty'):
            return None
        refused = self._refuseFiles(payload, max_bytes)
        if refused:
            mesh_preview.remove_files(payload)
            return await self._outline('preview_too_large', refused)
        self.progress.emit(self.tr('Loading the mesh preview…'))
        try:
            scene, precomputed = await vtk_run_in_thread(
                mesh_preview.scene_from_payload, payload)
        finally:
            await asyncio.to_thread(mesh_preview.remove_files, payload)
        self.precomputed = precomputed
        self.patchFaces = self._patchFaces(payload)
        self.shown = VOLUME if payload.get('volumes') else SURFACE
        if payload.get('decimated'):
            self.notice = {
                'kind': 'decimated',
                'text': self.tr('Preview decimated to {0:,} faces.').format(
                    int(payload.get('displayed_faces') or 0)),
                'patches': [], 'actions': []}
        return scene

    def _refuseFiles(self, payload: dict, max_bytes: int) -> str:
        """Why the worker's files must not be opened, or ''."""
        cache = mesh_preview.cache_dir(self._caseDir).resolve()
        total = 0
        for path, stated in mesh_preview.payload_files(payload):
            try:
                resolved = path.resolve()
                size = path.stat().st_size
            except OSError as error:
                return self.tr('Preview not built: {0}').format(error)
            if resolved.parent != cache:
                return self.tr('Preview not built: the worker wrote outside '
                               'the cache ({0}).').format(path)
            if stated and size != stated:
                return self.tr('Preview not built: {0} changed after the '
                               'worker wrote it.').format(path.name)
            total += size
        if total > max_bytes:
            return self.tr('Preview not built: its files take {0}, over the '
                           '{1} limit.').format(budget.format_bytes(total),
                                                budget.format_bytes(max_bytes))
        return ''

    @staticmethod
    def _patchFaces(payload: dict) -> dict:
        faces = {}
        for key, count in (payload.get('source_faces') or {}).items():
            region, category, name = key.split('\0', 2)
            if category != 'boundary':
                continue
            faces[f'{region}:{name}' if region else name] = int(count)
        return faces

    async def _outline(self, reason: str, text: str, *, refusal=False):
        """An empty scene; the box and the patch list stand in for it."""
        self.shown = 'outline'
        bounds = await asyncio.to_thread(
            mesh_preview.read_points_bounds, self._caseDir)
        if bounds is not None:
            self.outline = _outline_polydata(bounds)
        patches = [(str(name), int(count)) for name, count in
                   ((self.counts or {}).get('patches') or [])]
        self.patchFaces = {name: count for name, count in patches}
        actions = ['retry']
        if refusal and not self._buildAnyway:
            actions.append('build_anyway')
        self.notice = {'kind': 'outline', 'reason': reason, 'text': text,
                       'patches': patches, 'actions': actions}
        return {}


_RETRY_AS_SURFACE = object()
