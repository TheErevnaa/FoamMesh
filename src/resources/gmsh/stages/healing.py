"""The explicit OCC healing pass (Plan 48 RR, DP-4009).

Moved out of ``runner_v1.py`` so the batch run and the resident session heal
the imported shapes with one function."""
from __future__ import annotations


from .common import (MeshFailure)


class HealingStage:
    #: Repairs `occ.healShapes` can perform, in the order its signature takes
    #: them, paired with the healing key on the page that selects each one.
    HEAL_REPAIRS = (
        ('fixDegenerated', 'fixDegenerated'),
        ('fixSmallEdges', 'fixSmallEdges'),
        ('fixSmallFaces', 'fixSmallFaces'),
        ('sewFaces', 'sewFaces'),
        ('makeSolids', 'makeSolids'),
    )

    def heal_shapes(self, healing):
        """Run the explicit OCC repair pass, if the case asked for one.

        Plan 31, "Geometry kernels and healing". `gmsh.model.occ.healShapes`
        is what repairs a dirty STEP -- slivers, unsewn faces, degenerate
        seams -- and until now nothing in this application called it: the
        page offered the four import flags and no pass at all. An audit of
        this repository has carried "heal_shape() uncalled" as a standing
        defect since long before this plan.

        The pass performs exactly the repairs ticked on the healing page.
        Handing Gmsh its own defaults instead would turn on face sewing and
        degenerate-edge fixing, both of which are MEASURED here to destroy a
        good import -- sewing drops a solid to a face set, and fixing
        degenerate edges makes a sphere unmeshable.

        Returns the ``(before, after)`` entity counts when it ran, else None.
        """
        gmsh = self.gmsh
        if not healing.get('healShapes'):
            return None
        repairs = {name: bool(healing.get(key))
                   for name, key in self.HEAL_REPAIRS}
        if not any(repairs.values()):
            # The derivation already warns about this; the run says so too,
            # because a ledger line reading "applied" against a pass that
            # changed nothing is the exact confusion this register exists to
            # prevent.
            self.warnings.append(
                'the explicit healing pass was requested with no repair '
                'selected, so nothing was healed')
            self.ledger.record(
                'gmsh/healing/healShapes', True, False, applied=False,
                note='no repair selected, so the pass was not run')
            return None
        tolerance = float(healing.get('importTolerance', 1e-6) or 1e-6)
        before = self._entity_counts()
        try:
            gmsh.model.occ.healShapes(
                dimTags=[], tolerance=tolerance, **repairs)
        except Exception as error:                           # noqa: BLE001
            raise MeshFailure(
                f'the explicit healing pass failed: {error}') from error
        gmsh.model.occ.synchronize()
        after = self._entity_counts()
        changed = ', '.join(
            f'{dimension}D {before[dimension]} to {after[dimension]}'
            for dimension in sorted(before) if before[dimension] != after[dimension])
        self.ledger.record(
            'gmsh/healing/healShapes', True, True,
            note=(f'healed with {", ".join(sorted(name for name, on in repairs.items() if on))} '
                  f'at tolerance {tolerance:g}; '
                  + (f'entity counts {changed}' if changed
                     else 'entity counts unchanged')))
        if not changed:
            # Not a failure: a clean import has nothing to repair. Saying so
            # is the difference between "healing did nothing" and "healing
            # was never asked for", which the user cannot otherwise tell
            # apart from a mesh that came out the same.
            self.warnings.append(
                'the explicit healing pass found nothing to repair; the '
                'import already had no small edges, small faces or open '
                'shells that these settings address')
        self.topology_changes.append({
            'stage': 'healShapes',
            # Healing runs before `resolve_scopes`, so no prepared identity
            # has been bound to a tag yet and none needs remapping. The key
            # is present because every reader of this list indexes it.
            'entities': {},
            'repairs': sorted(name for name, on in repairs.items() if on),
            'tolerance': tolerance,
            'before': {str(key): value for key, value in before.items()},
            'after': {str(key): value for key, value in after.items()},
        })
        return before, after
