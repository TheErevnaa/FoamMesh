#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""GUI-side CAD import adapter.

Bridges the CAD core (read_cad -> tessellate -> split-by-face) into the same
``StlSurface`` objects the geometry pipeline already consumes, so a STEP/IGES/BREP
import flows through the existing base-grid/castellation/snap/layers/export path
unchanged. Mirrors StlImporter's interface (``load`` / ``identifyVolumes``).

Requires the ``[cad]`` extra at run time (read_cad/tessellate are OCCT). The
per-face split (surface_split) is headless and unit-tested.
"""
from __future__ import annotations

import re
from pathlib import Path

from .stl_utility import StlImporter, StlSurface, isClosed


def _sanitize(name: str) -> str:
    if not name:
        return name
    s = re.sub(r'\W+', '_', name, flags=re.ASCII)
    return ('_' + s) if s and s[0].isdigit() else s


class CadImporter(StlImporter):
    """Import STEP/IGES/BREP as tessellated per-face surfaces."""

    def load(self, files, params=None, unit=None):
        """Read *files*; *unit* is the length unit for a format that has none (BREP)."""
        self._stringIndices.clear()
        self._solids.clear()
        self._surfaceList.clear()
        self._bodies = []
        for f in files:
            for bodyName, surfaces in self._loadCADFile(Path(f), params, unit):
                self._bodies.append((bodyName, surfaces))
                self._solids.extend(surfaces)
                self._surfaceList.extend(surfaces)

    def identifyVolumes(self):
        """Closed bodies become volumes; open ones stay boundary surfaces.

        A STEP file says which faces make up which solid, so the import does
        not have to ask the user to pick them out again as it does for STL.
        The volume takes the body's own name where the file gave it one.
        """
        volumes, surfaces = [], []
        for bodyName, body in getattr(self, '_bodies', ()):
            if body and isClosed(body):
                for surface in body:
                    surface.volumeName = bodyName
                volumes.append(list(body))
            else:
                surfaces.extend(body)
        return volumes, surfaces

    def _loadCADFile(self, path: Path, params, unit=None):
        from foammesh.core.geometry.cad import read_cad, TessellationParams
        from foammesh.core.geometry.cad.tessellate import tessellate
        from foammesh.core.geometry.cad.surface_split import split_by_face_id_indexed

        from foammesh.core.geometry.units import to_metres

        shape, model = read_cad(path, unit)
        polydata = tessellate(shape, params or TessellationParams())
        # STEP and IGES declare their length unit and most CAD is written in
        # millimetres. The artifact store has always converted; this copy --
        # the one the viewport draws and the one the geometry database keeps --
        # did not, so a millimetre part was displayed and measured a thousand
        # times larger than the surface that would actually be meshed.
        polydata = to_metres(polydata, model.unit)
        names = {i: n for i, n in enumerate(model.patch_names())}
        bodyOfFace = {}
        faceIndex = 0
        for bodyIndex, body in enumerate(model.bodies):
            for _face in body.faces:
                bodyOfFace[faceIndex] = bodyIndex
                faceIndex += 1

        fName = _sanitize(path.stem)
        grouped: dict[int, list] = {}
        for fid, name, pd in split_by_face_id_indexed(polydata, names):
            n = pd.GetNumberOfCells()
            self._addArray(pd, 'fIndex', fName, n)
            sIndex = self._addArray(pd, 'sIndex', _sanitize(name), n)
            grouped.setdefault(bodyOfFace.get(fid, 0), []).append(
                StlSurface(pd, fName, _sanitize(name), sIndex))

        bodies = []
        for bodyIndex, surfaces in sorted(grouped.items()):
            body = model.bodies[bodyIndex] if bodyIndex < len(model.bodies) else None
            bodyName = _sanitize(getattr(body, 'name', '') or '')
            if not bodyName:
                bodyName = fName if len(grouped) == 1 else f'{fName}_{bodyIndex + 1}'
            bodies.append((bodyName, surfaces))
        return bodies
