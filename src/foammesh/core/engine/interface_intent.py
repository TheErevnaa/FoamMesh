#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""What an engine and a target can do with an interface pair's coupling intent.

Plan 42 SG22 Q2 (user decision E, 2026-10-11). A pair is authored before the
engine is chosen, and what the user states on it is an *intent*:

* ``conformal`` -- the two sides share one interface (fragmented, one mesh);
* ``non_conformal`` -- the two sides are meshed apart and coupled afterwards.

The Geometry page therefore offers both whatever engine the case will use.
It is this report, read at the engine's own page and run, that says whether
the chosen engine and target can honour the intent, and a pair that cannot be
honoured is refused there with the reason. Nothing falls back to the other
coupling silently.
"""

from __future__ import annotations

from typing import Iterable, Mapping

CONFORMAL = 'conformal'
NON_CONFORMAL = 'non_conformal'

#: How an engine honours a non-conformal intent (``non_conformal_route``).
ROUTE_NONE = 'none'
#: snappy writes the pair for OpenFOAM's non-conformal coupling itself.
ROUTE_STAGED_NCC = 'staged_ncc'
#: Gmsh meshes the two volumes apart (no fragment on the pair) and
#: ``createNonConformalCouples`` couples them after the OpenFOAM conversion.
ROUTE_SEPARATE_VOLUMES = 'separate_volumes'

#: What a non-conformal pair is called when nothing couples it.
UNMATCHED = 'unmatched surfaces'


def _text(row, *names) -> str:
    for name in names:
        if isinstance(row, Mapping) and row.get(name) not in (None, ''):
            return str(row[name])
    return ''


def _target_token(target_solver) -> str:
    return str(getattr(target_solver, 'value', target_solver) or '').lower()


def intent_report(engine_id: str, target_solver, pairs: Iterable) -> list[dict]:
    """One row per pair whose coupling is ``conformal`` or ``non_conformal``.

    ``pairs`` are the job-shaped rows (``coupling``, ``name``) or any mapping
    with those keys. A ``cyclic`` pair is the Periodic page's and is not
    judged here. Each row says what the user intended, whether this engine and
    target can honour it (``honoured``), whether the result is coupled
    (``coupled``) and the word to call the result by (``label``), with the
    ``refusal`` when it cannot be honoured.
    """
    from foammesh.core.engine.registry import ENGINE_REGISTRY

    try:
        engine = ENGINE_REGISTRY.get(str(engine_id))
    except Exception:                                    # noqa: BLE001
        engine = None
    route = getattr(engine, 'non_conformal_route', ROUTE_NONE)
    name_of_engine = (getattr(getattr(engine, 'descriptor', None),
                              'display_name', '') or str(engine_id))
    openfoam = _target_token(target_solver) == 'openfoam'
    rows = []
    for pair in pairs or ():
        intent = _text(pair, 'coupling') or CONFORMAL
        if intent not in (CONFORMAL, NON_CONFORMAL):
            continue
        name = _text(pair, 'name', 'pairId') or '<unnamed>'
        row = {'pair': name, 'intent': intent, 'engine': str(engine_id),
               'honoured': True, 'coupled': intent == CONFORMAL,
               'label': 'conformal' if intent == CONFORMAL else '',
               'refusal': '', 'reason': ''}
        if intent == NON_CONFORMAL:
            if route == ROUTE_NONE:
                row.update(
                    honoured=False, coupled=False, label=UNMATCHED,
                    refusal=(f'{name} is a non-conformal pair and '
                             f'{name_of_engine} cannot honour it: it has no '
                             'way to mesh the two sides apart and couple '
                             'them. Make the pair conformal on the Geometry '
                             'page or choose an engine that can.'))
            elif route == ROUTE_STAGED_NCC:
                row.update(coupled=True,
                           label='non-conformal (coupled)',
                           reason='written for OpenFOAM non-conformal '
                                  'coupling')
            elif openfoam:
                row.update(coupled=True,
                           label='non-conformal (coupled)',
                           reason='meshed apart, then coupled by '
                                  'createNonConformalCouples after the '
                                  'OpenFOAM conversion')
            else:
                row.update(coupled=False, label=UNMATCHED,
                           reason='meshed apart; only an OpenFOAM target '
                                  'couples them, so for this output the '
                                  'two sides are unmatched surfaces and no '
                                  'coupling is claimed')
        rows.append(row)
    return rows


def intent_refusal(engine_id: str, target_solver, pairs: Iterable) -> str:
    """The reason a run must be refused for these pairs, or ``''``."""
    reasons = [row['refusal'] for row
               in intent_report(engine_id, target_solver, pairs)
               if not row['honoured']]
    return ' '.join(reasons)
