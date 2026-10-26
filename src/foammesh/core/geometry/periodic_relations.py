"""Plan 49 SR3. A periodic pair edit, carried into its relation and checked.

The one path every periodic pair edit takes, whichever form it came through
(the Geometry page's ``cyclic`` interface pair, the Gmsh Periodic page's
pair) and whoever made it (the window or the local API): the facade calls
:func:`relate_periodic_edit` on the working copy before it commits.

1. The form row is carried into its periodic relation
   (``surface_intent.sync_periodic_relations``).
2. A side that is already a region interface, a blocking baffle, an internal
   face or a side of another pair is refused with the surface, the reason and
   the remedy (:class:`PeriodicSideConflict`); nothing is saved.
3. The pair is measured on the prepared geometry
   (:func:`periodic_match.periodic_match`) and the verdict is stored on the
   relation with its numbers and a digest of what was measured. A pair that
   does not map is *saved* with ``verification = failed``, which makes the
   relation unresolved (plan D7: a visibly invalid draft, blocked at run and
   export by SR7), so a pair being built up over several edits is never
   lost. The Geometry page asks before saving one (``InterfacePairPanel``).
"""
from __future__ import annotations

from typing import Mapping

from foammesh.db import surface_intent as model

from .periodic_match import periodic_match

__all__ = ('PeriodicSideConflict', 'relate_periodic_edit',
           'verify_periodic_relations')


class PeriodicSideConflict(ValueError):
    """A periodic pair names a side that is already something else."""

    def __init__(self, conflict: Mapping):
        super().__init__(conflict['message'])
        self.conflict = dict(conflict)


def _measure(prepared, relation) -> tuple[str, str, dict]:
    if not prepared:
        return ('unverified', 'the geometry is not prepared, so the pair '
                              'could not be measured', {})
    try:
        result = periodic_match(prepared, relation.get('ownerPatchUuid'),
                                relation.get('neighbourPatchUuid'), relation)
    except (OSError, ValueError, TypeError) as error:
        return 'unverified', f'the pair could not be measured: {error}', {}
    verdict = {True: 'verified', False: 'failed'}.get(result.verified,
                                                      'unverified')
    return verdict, result.reason, result.measures


def verify_periodic_relations(document, prepared: Mapping | None,
                              sources=None) -> list[dict]:
    """Measure the periodic relations of *document* on *prepared*.

    ``sources`` limits it to the relations edited through those form rows;
    ``None`` measures every periodic relation whose stored verdict is stale
    or missing. Writes ``verification``, ``verificationReason`` and
    ``verificationKey`` on each relation in place and returns
    ``[{relation_id, source, verification, reason, measures}]``.
    """
    results = []
    wanted = None if sources is None else {str(item) for item in sources}
    for _key, relation in sorted(
            ((document or {}).get('surfaceRelations') or {}).items(),
            key=lambda item: str(item[0])):
        if not isinstance(relation, dict) or \
                str(relation.get('kind') or '') != model.PERIODIC:
            continue
        source = str(relation.get('legacySource') or '')
        digest = model.periodic_verification_key(document, relation)
        if wanted is not None:
            if source not in wanted:
                continue
        elif (str(relation.get('verificationKey') or '') == digest
              and str(relation.get('verification') or '') != 'unverified'):
            continue
        verdict, reason, measures = _measure(prepared, relation)
        relation['verification'] = verdict
        relation['verificationReason'] = reason
        relation['verificationKey'] = digest
        results.append({'relation_id': relation.get('relationId'),
                        'source': source, 'verification': verdict,
                        'reason': reason, 'measures': measures})
    return results


def relate_periodic_edit(document, source: str,
                         prepared: Mapping | None) -> dict | None:
    """Carry the form row *source* into its relation, refuse or measure it.

    *document* is the working copy's content, edited in place. Raises
    :class:`PeriodicSideConflict` when a side is already something else.
    Returns the measurement of the pair (see :func:`verify_periodic_relations`)
    or ``None`` when the row is not a periodic pair.
    """
    model.sync_periodic_relations(document)
    _key, relation = model.relation_for_source(document, source)
    if relation is None:
        # Plan 49 SR7 (DP-5832). An affine Gmsh pair has no relation; its
        # sides obey the same rule.
        conflict = model.affine_side_conflict(document, source)
        if conflict is not None:
            raise PeriodicSideConflict(conflict)
        return None
    conflict = model.periodic_side_conflict(document, source)
    if conflict is not None:
        raise PeriodicSideConflict(conflict)
    if str(relation.get('transform') or '') not in ('translational',
                                                    'rotational'):
        return None
    results = verify_periodic_relations(document, prepared, (source,))
    return results[0] if results else None
