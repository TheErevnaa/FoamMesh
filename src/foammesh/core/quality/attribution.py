"""Which step to look at when the mesh quality fails (Plan 43 LY9, section 2).

When the quality check fails, only Quality turns red: Generate did its job and
stays done. What the red mark has to say is where to go next, so it names the
likely step and opens it: *Skewness 4.6 near face1: check Sizing or Layers.*

The answer is a heuristic and says so by its wording ("check ... or ..."), but
it is a fixed table, not a guess made at draw time: the same metric on the
same engine always names the same steps, so the sentence can be tested and
read back. A step is named by its task id, which is also the route the window
opens; the engine decides which task stands for "Sizing" and "Layers".
"""

from __future__ import annotations

#: The steps a metric points at, in the order they are named. The words are
#: the ribbon's own labels.
_RULES = (
    (('skew',), ('sizing', 'layers')),
    (('nonorth', 'non-orth', 'non orth', 'orthogonal'), ('sizing', 'layers')),
    (('aspect',), ('sizing', 'layers')),
    (('sicn', 'sige', 'gamma', 'quality'), ('sizing', 'layers')),
    (('volume', 'determinant', 'jacobian', 'concave', 'inverted'),
     ('geometry', 'sizing')),
)
_DEFAULT = ('sizing',)

#: Step word -> engine -> (task id, label shown).
_STEPS = {
    'sizing': {'snappy': ('snappy.base_grid', 'Sizing'),
               'gmsh': ('gmsh.global_sizing', 'Sizing')},
    'layers': {'snappy': ('snappy.layers', 'Layers'),
               'gmsh': ('gmsh.boundary_layers', 'Layers')},
    'geometry': {'snappy': ('setup.preparation', 'Preparation'),
                 'gmsh': ('setup.preparation', 'Preparation')},
}
_FAILED = frozenset({'fail', 'invalid'})


def attribute(verdict: dict | None, engine_id: str) -> dict | None:
    """The step(s) a failed verdict points at, or ``None`` when it did not fail.

    Returns ``{'sentence', 'steps': [{'task_id', 'label'}, ...]}``. A waived
    verdict was accepted by a person and points nowhere.
    """
    verdict = dict(verdict or {})
    if str(verdict.get('verdict') or '') not in _FAILED or verdict.get('waived'):
        return None
    engine = 'gmsh' if str(engine_id or '').lower() == 'gmsh' else 'snappy'
    measure = str(verdict.get('measure') or '').strip()
    detail = str(verdict.get('detail') or '').strip()
    lowered = measure.lower()
    words = _DEFAULT
    for needles, steps in _RULES:
        if any(needle in lowered for needle in needles):
            words = steps
            break
    steps = []
    for word in words:
        task_id, label = _STEPS[word][engine]
        if all(item['task_id'] != task_id for item in steps):
            steps.append({'task_id': task_id, 'label': label})
    named = ' or '.join(item['label'] for item in steps)
    what = ' '.join(part for part in (measure, detail) if part) or 'Quality'
    return {'sentence': f'{what}: check {named}.', 'steps': steps}
