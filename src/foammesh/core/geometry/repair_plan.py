"""Pure, deterministic readiness-to-repair plan builder."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4


ACTION_ORDER = (
    'tess.weld', 'tess.dedupe', 'tess.orient', 'tess.drop_fragments',
    'tess.fill_holes', 'tess.fix_nonmanifold', 'tess.collapse_slivers',
    'tess.detect_intersections')


def suggest(readiness_report: dict, route: str = 'tessellated') -> dict:
    requested = set()
    max_hole_size = None
    geometry_id = None
    revision = None
    for geometry in readiness_report.get('geometries', ()):
        geometry_id = geometry_id or geometry.get('geometry_id')
        revision = revision or geometry.get('revision')
        for finding in geometry.get('diagnostics', {}).get('findings', ()):
            if finding.get('count', 0) <= 0:
                continue
            requested.update(finding.get('repairable_by', ()))
            if finding.get('kind') == 'open_edges':
                max_hole_size = finding.get('characteristic_size')
    actions = []
    if route == 'cad':
        from .cad.healing_pipeline import CAD_ACTION_ORDER
        selected_geometry = next(iter(readiness_report.get('geometries', ())), {})
        bounds = selected_geometry.get('bbox') or ()
        diagonal = (sum((bounds[i * 2 + 1] - bounds[i * 2]) ** 2
                        for i in range(3)) ** .5 if len(bounds) == 6 else 1.0)
        diagnostics = selected_geometry.get('diagnostics', {}).get('findings', ())
        cad_finding = next((item for item in diagnostics
                            if item.get('kind') == 'cad_validity'), {})
        census = cad_finding.get('details', {}).get('census', {}).get(
            'tolerance_census', {})
        working = max(float(census.get('avg', 0) or 0), diagonal * 1e-6)
        size_finding = next((item for item in diagnostics
                             if item.get('kind') == 'small_features_vs_target'), {})
        target_size = size_finding.get('details', {}).get('target_cell_size')
        linear = (.1 if not target_size else max(diagonal * 1e-5,
                  min(diagonal * 1e-2, float(target_size) / 5)))
        actions = [
            {'action': action, 'params': (
                {'tolerance': working} if action in {
                    'cad.fix_shape', 'cad.sew', 'cad.fix_wireframe'} else
                {'linear_deflection': linear, 'angular_deflection_deg': 20}
                if action == 'cad.retessellate' else {}),
             'enabled': action not in {'cad.remove_small_faces', 'cad.unify_same_domain'}}
            for action in CAD_ACTION_ORDER]
    else:
        from .diagnostics.repair import TESSELLATED_ACTIONS

        for action in ACTION_ORDER:
            if action not in requested:
                continue
            # Plan 26 WP7.2/WP7.4. A band-0 action reports and changes nothing,
            # so it is not part of a *plan*: including it would make a
            # `wrap_recommended` geometry look as though a one-click repair
            # would help. It reaches the user through its finding's repair
            # actions instead, which is what naming it in `repairable_by`
            # bought -- discovery, not a false promise of a fix.
            if TESSELLATED_ACTIONS[action].band == 0:
                continue
            params = {}
            if action == 'tess.fill_holes' and max_hole_size:
                params['max_hole_size'] = float(max_hole_size)
            actions.append({'action': action, 'params': params, 'enabled': True})
    canonical = {'geometry_id': geometry_id, 'base_revision': revision,
                 'route': route, 'actions': actions}
    digest = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'plan_id': f'rp-{uuid4().hex}', **canonical,
        'digest': f'sha256:{digest}',
    }
