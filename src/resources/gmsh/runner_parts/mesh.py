"""Mesh part of the batch runner (Plan 48 GM7).

``intent['meshActions']`` is a list of ``{'action': id, 'values': {...}}``.
Each one is played once, after the last mesh pass of the job
(``after_mesh_pass`` for ``run.generate_dimension``), in the order given and
before the runner raises the element order for export. The ids and values are
those of ``core/gmsh/generate_actions.py``'s job actions:

``set_order``              ``order`` (1-10) and ``options`` ({name: number})
``regularize``             ``algorithm``, ``options``, ``force``, ``niter``
``reverse_elements``       ``elements`` (tags)
``reverse_entities``       ``entities`` ([[dim, tag], ...])
``delete_elements``        ``elements``
``delete_entity_mesh``     ``entities``
``reclassify_subset``      ``angle``, ``elements``, ``entities``, ...
``optimize_quad_topology`` and ``untangle_geometry``

The work is the module-level functions of ``session_mesh_v1`` (the resident
session runs the same ones), so a job and the window do the same thing. A
failing action stops the job with its reason: a mesh the person asked to
have edited and was not edited is not a result.
"""
from __future__ import annotations

import os
import sys


def _actions(run):
    intent = getattr(run, 'intent', None) or {}
    listed = intent.get('meshActions') or []
    return [item for item in listed if isinstance(item, dict)]


def apply_mesh_actions(run, dimension=None, **_context):
    """Play the job's mesh actions after its last mesh pass."""
    if dimension is not None and dimension != getattr(
            run, 'generate_dimension', dimension):
        return
    actions = _actions(run)
    if not actions or getattr(run, '_gm7_done', False):
        return
    run._gm7_done = True
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    import session_mesh_v1
    done = []
    for item in actions:
        name = str(item.get('action', ''))
        try:
            answer = session_mesh_v1.apply_job_action(
                run.gmsh, name, item.get('values') or {})
        except session_mesh_v1.MeshActionError as error:
            raise RuntimeError(f'mesh action {name}: {error}') from error
        done.append({'action': name, 'result': answer})
    statistics = getattr(run, 'statistics', None)
    if isinstance(statistics, dict):
        statistics['meshActions'] = done


def register(add_hook):
    add_hook('after_mesh_pass', apply_mesh_actions)
