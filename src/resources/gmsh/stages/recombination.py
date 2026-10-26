"""Quadrangle recombination marks (Plan 48 RR)."""
from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from quantities import count_text

from .common import TESTED_RECOMBINERS


class RecombinationStage:
    def apply_recombination(self):
        """Quads on the surfaces, and the cell family that follows from them.

        Plan 30 WP12. ``Mesh.RecombineAll`` alone is not enough: it asks the
        2D pass to recombine, but a surface the mesher decides against is left
        as triangles and nothing says so. Each surface is therefore marked
        explicitly with ``setRecombine`` and the count is recorded, so a run
        that recombined nine faces of ten is visible instead of being reported
        as a quad mesh.

        The derivation has already refused this for every target but SU2, so
        this method only ever runs on the route whose reader accepts the
        result.
        """
        algorithms = self.intent.get('algorithms') or {}
        if not algorithms.get('recombine'):
            return
        gmsh = self.gmsh
        self.set_number('Mesh.RecombineAll', 1,
                        control='gmsh/algorithms/recombine')
        # Which recombiner, chosen rather than assumed. MEASURED on
        # duct.step at a 0.04 m target: 0 gave 306 quadrangles and left 70
        # triangles, 1 gave 356 and left none, 2 gave 460, 3 gave 408. Four
        # values, four different meshes -- and 4 read back as 4 and then
        # meshed nothing at all, surface and volume both empty, without
        # raising, while 9 was clamped to 0 in silence. So anything outside
        # the qualified four is refused here with its reason rather than
        # handed to Gmsh to see what happens.
        recombiner = int(algorithms.get('recombinationCode', 1) or 0)
        if recombiner not in TESTED_RECOMBINERS:
            self.warnings.append(
                f'recombination algorithm {recombiner} was asked for and is '
                'not one this product has qualified; Gmsh takes the value and '
                'measured meshes came back empty, so blossom was used instead')
            self.ledger.record('gmsh/algorithms/recombinationAlgorithm',
                               recombiner, 1, applied=False,
                               note='qualified values are '
                                    + ', '.join(str(item) for item
                                                in sorted(TESTED_RECOMBINERS)))
            recombiner = 1
        self.set_number('Mesh.RecombinationAlgorithm', recombiner,
                        control='gmsh/algorithms/recombinationAlgorithm')
        surfaces = [int(tag) for _dim, tag in gmsh.model.getEntities(2)]
        touched = 0
        for tag in surfaces:
            try:
                gmsh.model.mesh.setRecombine(2, tag)
                touched += 1
            except Exception as error:
                self.warnings.append(
                    f'surface {tag} could not be marked for recombination: '
                    f'{error}')
        self.ledger.record('gmsh/algorithms/recombine.surfaces',
                           len(surfaces), touched,
                           applied=bool(touched),
                           note=f'{touched:,} of '
                                f'{count_text(len(surfaces), "surface")} '
                                'marked for recombination')
        self.statistics['recombination'] = {
            'surfaces': len(surfaces), 'recombined': touched,
            'algorithm': recombiner}
