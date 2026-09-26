"""Which quality checks a saved Mesh setup selection requires, in order.

Plan 33 QA-06. Three routes are supported and each needs a different ordered
list of checks before a mesh may be called qualified:

============================  ====================================
Saved Mesh setup selection    Checks that must complete
============================  ====================================
OpenFOAM, Gmsh                the native Gmsh element check on the
                              native mesh, then checkMesh on the
                              converted polyMesh
SU2, Gmsh                     the native Gmsh element check, then
                              the SU2 readiness check on the
                              exported artifact and its source
OpenFOAM, snappyHexMesh       checkMesh alone
============================  ====================================

The list is derived here, from the two saved keys and nothing else, because
the fault this module exists to stop was a dispatch that read the page it had
been pressed on. A page is where a user is standing; the selection is what the
project asked for, and only one of those is evidence.

Two rules follow from the table and are enforced by the run that reads it.

**One passing check never masks a missing one.** A route is ``PASSED`` only
when every check it names has run and passed. A check that never ran is
``missing``, which is not a failure and not a pass -- it is the absence of
evidence, and the route stays ``INCOMPLETE`` until it is there.

**A result is never relabelled for a route that did not ask for it.** The
record carries the route it was reached under, so an unchanged native result
can be carried forward for the same mesh on the same route and cannot be
carried across a change of route.
"""
from __future__ import annotations

import json
from pathlib import Path

from foammesh.core.quality.checkmesh_service import (
    NATIVE_CHECK, SU2_READINESS_CHECK,
)

#: The generator's own element gate, filed during the meshing run rather than
#: by a check the user presses. It is required evidence all the same: nothing
#: else measures the elements Gmsh actually produced.
NATIVE_GMSH_CHECK = 'gmsh-native'

#: Where the route record lives, beside the per-check report slots it indexes.
ROUTE_RECORD_PATH = 'foammesh/quality/route-checks.json'

#: What each check is called on screen. Sentence case, one name per check.
CHECK_LABELS = {
    NATIVE_GMSH_CHECK: 'Gmsh element quality',
    NATIVE_CHECK: 'checkMesh',
    SU2_READINESS_CHECK: 'SU2 readiness',
}

#: The three supported routes, written out rather than derived, so that the
#: table above and the code cannot drift apart silently.
ROUTE_CHECKS = {
    ('openfoam', 'gmsh'): (NATIVE_GMSH_CHECK, NATIVE_CHECK),
    ('su2', 'gmsh'): (NATIVE_GMSH_CHECK, SU2_READINESS_CHECK),
    ('openfoam', 'snappy'): (NATIVE_CHECK,),
}

#: Which engines file a native gate of their own, read off the table above
#: rather than written out again. The facade is forbidden to decide anything
#: by comparing an engine name (F-14), and this is the reason the rule is a
#: good one: the question is never "is this Gmsh?" but "does this engine
#: measure its own elements?", and the route table already answers it.
NATIVE_GATE_CHECKS = {
    engine: checks[0]
    for (_target, engine), checks in ROUTE_CHECKS.items()
    if checks[0] == NATIVE_GMSH_CHECK
}

#: A route is settled only when every check it named passed.
PASSED = 'PASSED'
FAILED = 'FAILED'
INCOMPLETE = 'INCOMPLETE'


def _token(value) -> str:
    return str(getattr(value, 'value', value) or '').strip().lower()


def terminal_check(target_solver) -> str:
    """The check that answers "is this mesh good?" for this solver.

    The counterpart of :func:`core.engine.base.qa_operation`, in check names
    rather than operation names, and with the same permissive default: a
    project that never chose a solver is an OpenFOAM project.
    """
    return (SU2_READINESS_CHECK if _token(target_solver) == 'su2'
            else NATIVE_CHECK)


def quality_checks_for(target_solver, engine_id=None) -> tuple[str, ...]:
    """The ordered checks this saved selection requires before progression.

    An engine that has no native gate contributes no row, so a project that
    never chose one -- every project written before Plan 28 reads that way --
    asks for exactly the single check it always asked for.
    """
    target = _token(target_solver)
    engine = _token(engine_id)
    known = ROUTE_CHECKS.get((target, engine))
    if known is not None:
        return known
    terminal = terminal_check(target)
    native = NATIVE_GATE_CHECKS.get(engine)
    if native is not None:
        return (native, terminal)
    return (terminal,)


def route_key(target_solver, engine_id=None) -> str:
    """One string naming the selection a record was reached under."""
    return '{0}/{1}'.format(_token(target_solver) or 'unselected',
                            _token(engine_id) or 'unselected')


def record_path(case_path) -> Path:
    return Path(case_path) / ROUTE_RECORD_PATH


def load_route_record(case_path) -> dict | None:
    """The last route record filed for this case, or ``None``."""
    try:
        document = json.loads(
            record_path(case_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def save_route_record(case_path, record: dict) -> Path:
    path = record_path(case_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True, default=str) + '\n',
        encoding='utf-8')
    return path


def compose_verdict(entries) -> str:
    """``PASSED`` only when every required check ran and passed."""
    verdicts = [str(entry.get('verdict') or '') for entry in entries]
    if not verdicts:
        return INCOMPLETE
    if any(verdict == 'missing' for verdict in verdicts):
        return INCOMPLETE
    if all(verdict == 'passed' for verdict in verdicts):
        return PASSED
    return FAILED


def reusable_entry(record, route: str, check: str,
                   mesh_identity: str) -> dict | None:
    """The stored entry for *check* that still stands, or ``None``.

    Three things have to agree: the route it was reached under, the check it
    answered, and the mesh it answered about. A change in any of them makes
    the old entry a statement about something else, and carrying it forward
    would be relabelling rather than reuse.
    """
    if not isinstance(record, dict) or str(record.get('route') or '') != route:
        return None
    if not mesh_identity:
        return None
    for entry in record.get('checks') or ():
        if not isinstance(entry, dict) or entry.get('check') != check:
            continue
        if str(entry.get('mesh_identity') or '') != str(mesh_identity):
            return None
        if entry.get('verdict') == 'missing':
            return None
        carried = dict(entry)
        carried['status'] = 'reused'
        return carried
    return None


def route_verdict(case_path, target_solver, engine_id=None) -> str:
    """What the filed record says about the route this project asks for now.

    A record reached under a different selection, or one that no longer names
    the checks the selection asks for, answers ``INCOMPLETE``: it is evidence
    about another route, and this one has none yet.
    """
    record = load_route_record(case_path)
    if record is None:
        return INCOMPLETE
    if str(record.get('route') or '') != route_key(target_solver, engine_id):
        return INCOMPLETE
    required = quality_checks_for(target_solver, engine_id)
    if tuple(record.get('required') or ()) != required:
        return INCOMPLETE
    verdict = str(record.get('verdict') or '')
    return verdict if verdict in (PASSED, FAILED, INCOMPLETE) else INCOMPLETE


__all__ = [
    'CHECK_LABELS', 'FAILED', 'INCOMPLETE', 'NATIVE_GMSH_CHECK', 'PASSED',
    'ROUTE_CHECKS', 'ROUTE_RECORD_PATH', 'compose_verdict',
    'load_route_record', 'quality_checks_for', 'record_path', 'reusable_entry',
    'route_key', 'route_verdict', 'save_route_record', 'terminal_check',
]
