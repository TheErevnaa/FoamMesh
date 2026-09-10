"""Grey the editors the current configuration cannot reach.

Plan 31 CP-09 item 4: "use conditional editors from shared metadata; preserve
inactive settings visibly where useful, but ensure they do not affect the job
unexpectedly."

The metadata is :attr:`FieldDescriptor.applies_when`, which the AF2 registry
has published on 42 of its 149 fields since it was written and which no view
module has ever read. MEASURED before this change: the Gmsh Compute page
renders ``gmsh.compute.element_order`` -- which no export adapter accepts at
order 2, gmshToFoam reading first-order MSH 2.2 and nothing else and the SU2
reader holding the linear type codes only -- as a live spin box on an
OpenFOAM case, and pressing Update writes it and stales the mesh for a value
no run will read. (C31-14d: the clause that field carried was itself wrong,
naming SU2 as the route that allowed order 2. It is now the two exporters
that refuse it.)

Two rules, both here so the two page families cannot drift apart:

* an editor whose clauses do not hold is disabled, labelled "(inactive)" and
  carries the sentence saying which setting would make it live again; and
* its field is dropped from the pending patch, so an inactive control cannot
  write from off screen.
"""
from __future__ import annotations

from foammesh.core.facade import applicability


def refresh_applicability(client, editors, pending=None) -> dict:
    """Judge every editor, grey the inactive ones, clean the patch.

    Returns ``{field_id: reason}`` for the fields that do not apply, which is
    what a page needs to say "3 settings on this page are inactive" without
    asking each editor again.
    """
    values, titles = condition_context(client, editors)
    inactive: dict[str, str] = {}
    for field_id, editor in editors.items():
        clauses = getattr(editor.descriptor, 'applies_when', ()) or ()
        verdict = applicability.evaluate(clauses, values, titles=titles)
        editor.setApplicability(verdict.applies, verdict.reason)
        if not verdict.applies:
            inactive[field_id] = verdict.reason
            if pending is not None:
                pending.pop(field_id, None)
    return inactive


def condition_context(client, editors) -> tuple[dict, dict]:
    """The values and the display names the clauses on this page refer to.

    One lookup per referenced field, not per editor: a Gmsh task page carries
    up to eleven fields all conditioned on the same ``mesh.engine``.
    """
    referenced: list[str] = []
    for editor in editors.values():
        clauses = getattr(editor.descriptor, 'applies_when', ()) or ()
        for field_id in applicability.referenced_fields(clauses):
            if field_id not in referenced:
                referenced.append(field_id)
    values: dict = {}
    titles: dict = {}
    for field_id in referenced:
        try:
            descriptor = client.descriptor(field_id)
        except Exception:                                    # noqa: BLE001
            descriptor = None
        title = getattr(descriptor, 'title', '')
        if title:
            titles[field_id] = title
        try:
            values[field_id] = client.field_values((field_id,))[field_id]
        except Exception:                                    # noqa: BLE001
            # Unreadable is not false: `applicability.evaluate` leaves a
            # clause it cannot judge alone rather than greying the control.
            continue
    return values, titles
