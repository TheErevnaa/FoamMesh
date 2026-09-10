"""Lazy native CAD findings used alongside tessellation diagnostics."""
from __future__ import annotations

from .checks import Finding, Severity


def check_cad(shape, *, tolerance: float = 1e-6, pair_limit: int = 200):
    from foammesh.core.geometry.cad.healing_pipeline import OcctHealingBackend, _analyze
    census = _analyze(shape, tolerance)
    free = census['free_closed_wires'] + census['free_open_wires']
    invalid = int(not census['valid'])
    validity_count = invalid + free
    findings = [Finding(
        'cad_validity', validity_count,
        Severity.ERROR if validity_count else Severity.OK,
        ('CAD B-Rep is valid and has no free wires.' if not validity_count else
         f"CAD B-Rep has {free} free wire(s); valid={census['valid']}."),
        repairable_by=('cad.fix_shape', 'cad.sew', 'cad.fix_wireframe'),
        engine_impact={'snappy': 'Invalid or unsewn B-Rep creates a leaking tessellation.'},
        details={'census': census})]
    small = census['small_faces'] + census['small_edges']
    findings.append(Finding(
        'cad_small_features', small,
        Severity.WARNING if small else Severity.OK,
        f"{census['small_faces']} small CAD face(s), {census['small_edges']} small edge(s).",
        characteristic_size=min((value for value in (
            census['face_area_min'], census['edge_length_min']) if value is not None),
                                default=None),
        repairable_by=('cad.fix_wireframe', 'cad.remove_small_faces') if small else (),
        engine_impact={'snappy': 'Sub-cell CAD details can produce noisy surface refinement.'},
        details={'census': census}))

    backend = OcctHealingBackend()
    bodies = backend.split_bodies(shape)
    overlaps, evaluated = [], 0
    if len(bodies) > 1:
        from OCC.Core.BRepExtrema import BRepExtrema_DistShapeShape
        for left in range(len(bodies)):
            for right in range(left + 1, len(bodies)):
                if evaluated >= pair_limit:
                    break
                evaluated += 1
                distance = BRepExtrema_DistShapeShape(bodies[left], bodies[right])
                distance.Perform()
                if distance.IsDone() and float(distance.Value()) <= tolerance:
                    overlaps.append({'body_a': left, 'body_b': right,
                                     'distance': float(distance.Value())})
            if evaluated >= pair_limit:
                break
    findings.append(Finding(
        'overlapping_shells', len(overlaps),
        Severity.ERROR if overlaps else Severity.OK,
        f'{len(overlaps)} contacting/overlapping CAD body pair(s).',
        repairable_by=(), evaluated=evaluated < pair_limit,
        engine_impact={'snappy': 'Overlapping bodies are a canonical wrap candidate.'},
        details={'pairs': overlaps[:50], 'pairs_evaluated': evaluated,
                 'pair_limit': pair_limit}))
    return findings
