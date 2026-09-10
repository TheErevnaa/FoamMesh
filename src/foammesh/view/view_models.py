#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""View-models: turn core results into display-ready rows for the GUI panels.

These are plain Python (no Qt) so they are unit-testable. The actual Qt widgets
(diagnostics panel, QA dashboard, agent-changes panel, bbox label) bind to these,
which keeps all panel *logic* tested even though the visual widget needs a display.
"""
from __future__ import annotations

from foammesh.core.geometry.units import si_factor
from foammesh.core.quality import (
    parse_checkmesh, readiness_verdict, estimate_cell_count, estimate_memory_gb,
)


# --- bounding-box label ---------------------------------------------------

def bbox_display(bbox, unit: str = 'm') -> dict:
    if bbox is None:
        return {'available': False}
    f = si_factor(unit)
    sx, sy, sz = bbox.size
    return {
        'available': True,
        'min': tuple(round(v / f, 6) for v in (bbox.xmin, bbox.ymin, bbox.zmin)),
        'max': tuple(round(v / f, 6) for v in (bbox.xmax, bbox.ymax, bbox.zmax)),
        'size': tuple(round(v / f, 6) for v in (sx, sy, sz)),
        'diagonal': round(bbox.diagonal / f, 6),
        'unit': unit,
    }


# --- geometry diagnostics panel ------------------------------------------

def diagnostics_rows(health) -> dict:
    return {
        'score': health.score,
        'watertight': health.watertight,
        'verdict': health.verdict,
        'rows': [
            {'check': f.kind, 'count': f.count,
             'severity': f.severity.value, 'message': f.message}
            for f in health.findings
        ],
    }


# --- mesh-QA dashboard ----------------------------------------------------

def qa_dashboard(checkmesh_log: str, *, base_cells: int | None = None,
                 refinement_levels: list[int] | None = None) -> dict:
    result = parse_checkmesh(checkmesh_log)
    verdict = readiness_verdict(result)
    out = {
        'metrics': result.to_dict(),
        'ready': verdict.ok,
        'reasons': verdict.reasons,
    }
    if base_cells is not None:
        cells = estimate_cell_count(base_cells, refinement_levels or [])
        out['estimate'] = {'cells': cells, 'memory_gb': round(estimate_memory_gb(cells), 2)}
    return out


# --- CAD assembly panel ---------------------------------------------------

def cad_tree_rows(model) -> dict:
    """Display data for the CAD assembly tree (bodies -> faces, with names/colors)."""
    return {
        'format': model.source_format,
        # R193. What the file declared is what the user recognises; the
        # reader's own unit is an implementation detail of the import.
        'unit': getattr(model, 'declared_unit', '') or model.unit,
        'read_unit': model.unit,
        'n_bodies': model.n_bodies,
        'n_faces': model.n_faces,
        'bodies': [
            {
                'id': b.id, 'name': b.name, 'color': b.color,
                'faces': [
                    {'id': f.id, 'name': f.name, 'patch': f.patch, 'color': f.color}
                    for f in b.faces
                ],
            }
            for b in model.bodies
        ],
    }


def tessellation_params_display(params) -> dict:
    return {
        'linear_deflection': params.linear_deflection,
        'angular_deflection_deg': params.angular_deflection_deg,
        'relative': params.relative,
        'parallel': params.parallel,
    }


# --- agent changes panel --------------------------------------------------

def agent_changes_rows(proposals) -> list[dict]:
    rows = []
    for p in proposals:
        rows.append({
            'proposal_id': p.proposal_id,
            'title': p.title,
            'strategy': p.strategy,
            'status': p.status.value,
            'items': [
                {'action': i.action, 'target': i.target,
                 'reason': i.reason, 'effect': i.effect}
                for i in p.items
            ],
        })
    return rows
