"""Deterministic, advice-only mapping from checkMesh failures to remedies."""
from __future__ import annotations


def recommendations(result) -> list[dict]:
    details = list(result.failed_check_details or result.warnings)
    output = []
    for detail in details:
        text = detail.lower()
        if 'negative' in text or 'zero volume' in text:
            actions = ['remove_cells', 'collapse_short_edges']
            advice = 'Remove invalid cells, then regenerate or locally improve the source mesh.'
        elif 'skew' in text:
            actions = []
            advice = ('Increase local surface refinement or adjust snapping; no post-mesh '
                      'utility reliably fixes clustered high skewness.')
        elif 'non-orth' in text:
            actions = ['smooth_points']
            advice = 'Preview point smoothing; if it regresses volume, refine and regenerate.'
        elif 'open' in text or 'illegal face' in text or 'topolog' in text:
            actions = ['remove_faces', 'stitch_patches']
            advice = 'Inspect the failed face set and repair or stitch topology after a preview.'
        elif 'short edge' in text:
            actions = ['collapse_short_edges']
            advice = 'Preview collapsing the reported short edges.'
        else:
            actions = []
            advice = 'No automated remedy is known; inspect the reported set and source controls.'
        output.append({'failure': detail, 'actions': actions, 'advice': advice,
                       'automated_remedy': bool(actions)})
    if not output:
        output.append({
            'failure': 'No parsed failure details', 'actions': [],
            'advice': ('Run Mesh Check with failed-set output enabled; no automated remedy '
                       'can be selected without a classified failure.'),
            'automated_remedy': False,
        })
    return output
