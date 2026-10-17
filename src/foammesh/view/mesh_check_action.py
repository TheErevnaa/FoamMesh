"""The one checkMesh action (Plan 42 SG18).

Three doors used to reach the mesh check by three routes: the Quality page's
buttons, the footer's stage run, and the Mesh menu / tab's dashboard. Each
resolved which check judges the mesh on its own and submitted it on its own.
They now share this module: one rule for which operation runs
(:func:`check_operation`, :func:`operation_from_listing`) and one submit
(:func:`run_mesh_check`). What a door does with the result (a status note, a
progress modal, the console table and report dialog) stays with that door.

An explicit *operation* is the user's choice, not the target's: Gmsh's SU2
readiness button runs ``quality.su2_readiness`` whatever the target solver
says (GN16).
"""

from __future__ import annotations

from foammesh.view.facade_client import query, submit


def operation_from_listing(payload) -> str:
    """The check a ``mesh.engine.list`` answer names for this project.

    The rule lives on the engine seam (F-24); the facade's own answer is
    preferred when it gave one, and anything unreadable means checkMesh.
    """
    from foammesh.core.engine.base import qa_operation

    payload = dict(payload or {})
    return str(payload.get('qa_operation')
               or qa_operation(payload.get('target_solver')))


def check_operation(client) -> str:
    """Which check judges this project's mesh, asked of the facade."""
    from foammesh.core.engine.base import qa_operation

    try:
        result = query(client, 'mesh.engine.list')
    except Exception:                                        # noqa: BLE001
        return qa_operation(None)
    return operation_from_listing(getattr(result, 'payload', None))


def run_mesh_check(client, on_result, *, operation: str | None = None,
                   parameters: dict | None = None):
    """Submit one mesh check and hand its result to *on_result*.

    *operation* is the user's explicit choice; without one the target's
    check runs. Returns what :func:`submit` returns (the scheduled task, or
    ``None`` when it ran synchronously).
    """
    return submit(client, operation or check_operation(client),
                  dict(parameters or {}), then=on_result)
