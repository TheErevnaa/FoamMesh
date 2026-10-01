"""Plan 37 F-2: did a snappy run keep one connected mesh per seed?

MEASURED (``plans/evidence/plan37/uf16-v13-exclude-points.md``, live case C):
with exclude points (``outsidePoints``) OpenFOAM 13 switches to "keep every
space without an exclude point", and the cut-off pieces of the excluded space
stay in the mesh as separate regions -- 34,375 cells in 1,182 regions, the
largest 24,628 cells, the rest mostly one cell each, 208 to 4,415 regions
across the runs. checkMesh says ``*Number of regions: 1182`` but does not
count it among its failed checks, so the run graded runnable and the Quality
task passed with the pieces in it.

This module reads the region count (from the checkMesh log, else from the
mesh's own face connectivity), compares it with what the seeds ask for and,
when the mesh is in more pieces, files it as a blocking finding with a plain
sentence: how many regions, the probable cause, and what to do. FoamMesh does
not remove the pieces itself (user decision 2026-10-01): which piece is wanted
is the user's call.

The pre-run estimate is written at launch by the snappy seed gate: the voxel
volume of the spaces the seeds keep and of the spaces the exclude points
remove, so the retained mesh can be set beside it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from foammesh.core.quantities import count_text

logger = logging.getLogger(__name__)

#: Where the launch-time estimate is kept, beside ``latest.json``.
ESTIMATE_NAME = 'retained-estimate.json'
#: The connectivity fallback reads owner/neighbour. There is no fixed cell
#: cap (2026-10-01: a mesh may go beyond 150 M cells when the RAM holds it):
#: it is skipped only when the RAM its read and graph need is not free --
#: this many times the two files' bytes (the text and its parse) ...
FALLBACK_FILE_FACTOR = 3
#: ... plus this many bytes a label (the int64 arrays, the sparse graph in
#: both directions and the component labels).
FALLBACK_BYTES_PER_LABEL = 64
#: An ASCII label line is about this many bytes, for counting the labels
#: from the file sizes before reading them.
ASCII_BYTES_PER_LABEL = 8

#: The blocking finding's leading words, for a reader that needs to tell it
#: from checkMesh's own findings (it is the app's judgement, not a check).
FINDING_PREFIX = 'mesh split into '


def estimate_path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'quality' / ESTIMATE_NAME


def write_estimate(case_path, estimate: dict | None) -> None:
    """Keep the launch-time estimate; ``None`` removes a stale one."""
    path = estimate_path(case_path)
    try:
        if estimate is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(estimate, indent=2), encoding='utf-8')
    except OSError as error:
        logger.warning('retained-region estimate not kept: %s', error)


def read_estimate(case_path) -> dict | None:
    try:
        data = json.loads(estimate_path(case_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def estimate(seeds, excludes, field=None) -> dict:
    """What the launch expects to keep.

    *seeds* are ``(label, type, xyz)``, *excludes* ``(name, xyz)`` and
    *field* the Plan 36 voxel labelling (anything with ``space_at``) or
    ``None``. The seeded volume is the spaces the seeds alone keep; the
    excluded volume the spaces holding an exclude point and no seed -- the
    seed wins in OpenFOAM 13.
    """
    seeds = list(seeds or ())
    excludes = list(excludes or ())

    def spaces(points) -> dict:
        found_spaces = {}
        if field is None:
            return found_spaces
        for point in points:
            try:
                found = field.space_at(point)
            except Exception:  # noqa: BLE001 - an unplaceable point
                found = None
            label = int(getattr(found, 'label', 0) or 0) if found else 0
            if label:
                found_spaces[label] = float(
                    getattr(found, 'volume', 0.0) or 0.0)
        return found_spaces

    kept = spaces(point for _label, _kind, point in seeds)
    removed = {label: volume for label, volume in
               spaces(point for _name, point in excludes).items()
               if label not in kept}
    return {
        'seeds': len(seeds),
        'excludes': [{'name': str(name), 'point': [float(v) for v in point]}
                     for name, point in excludes],
        'seeded_spaces': len(kept),
        'seeded_volume': sum(kept.values()) if kept else None,
        'excluded_volume': sum(removed.values()) if removed else None,
    }


def fallback_memory(mesh_dir) -> int:
    """Bytes of RAM :func:`count_cell_regions` needs for this ``polyMesh``."""
    total = 0
    for name in ('owner', 'neighbour'):
        for candidate in (Path(mesh_dir) / name, Path(mesh_dir) / f'{name}.gz'):
            if candidate.is_file():
                total += candidate.stat().st_size
                break
    labels = total // ASCII_BYTES_PER_LABEL
    return FALLBACK_FILE_FACTOR * total + FALLBACK_BYTES_PER_LABEL * labels


def count_cell_regions(case_path, *, max_cells=None):
    """Connected cell regions of ``constant/polyMesh``, or ``None``.

    The fallback for a log that did not say: owner and neighbour only, ASCII
    only. It has no cell cap unless *max_cells* names one; it is skipped
    (``None``, and logged with the RAM it needs and the RAM free) only when
    the free RAM cannot hold its read. Returns ``(regions, sizes)`` with the
    sizes largest first.
    """
    try:
        import numpy as np
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        from foammesh.core.mesh.poly_mesh_boundary import _read_labels

        from foammesh.support.resource_budget import memory_refusal

        mesh = Path(case_path) / 'constant' / 'polyMesh'
        refusal = memory_refusal('counting the cell regions',
                                 fallback_memory(mesh))
        if refusal is not None:
            logger.info('cell regions not counted from the mesh: %s',
                        refusal)
            return None
        owner = _read_labels(mesh, 'owner')
        neighbour = _read_labels(mesh, 'neighbour')
        if owner.size == 0:
            return None
        cells = int(owner.max()) + 1
        if neighbour.size:
            cells = max(cells, int(neighbour.max()) + 1)
        if max_cells is not None and cells > max_cells:
            return None
        internal = owner[:neighbour.size]
        graph = coo_matrix(
            (np.ones(neighbour.size, dtype=np.int8), (internal, neighbour)),
            shape=(cells, cells))
        count, labels = connected_components(graph, directed=False)
        sizes = sorted((int(v) for v in np.bincount(labels)), reverse=True)
        return int(count), sizes
    except Exception as error:  # noqa: BLE001 - binary, missing, malformed
        logger.info('cell regions not counted from the mesh: %s', error)
        return None


def assess(result, *, seeds: int, excludes, estimate: dict | None = None,
           counted=None) -> dict | None:
    """The retained-region judgement for one checked snappy mesh, or ``None``.

    *result* is a parsed ``CheckMeshResult``; *seeds* the number of region
    seeds; *excludes* the exclude-point names; *counted* the fallback's
    ``(regions, sizes)`` when the log had no count. ``None`` when the region
    count is not known.
    """
    regions = getattr(result, 'regions', None)
    sizes = list(getattr(result, 'region_cells', None) or ())
    source = 'checkMesh'
    if regions is None and counted:
        regions, sizes, source = counted[0], list(counted[1]), 'mesh'
    if regions is None:
        return None
    regions = int(regions)
    expected = max(1, int(seeds or 0))
    cells = getattr(result, 'cells', None)
    if cells is None and sizes and len(sizes) == regions:
        cells = sum(sizes)
    retained = (sum(sizes[:expected])
                if sizes and len(sizes) == regions else None)
    excludes = [str(name) for name in excludes or ()]
    split = regions > expected
    if not split:
        cause = None
    elif excludes and seeds:
        cause = 'seeds_and_excludes'
    elif excludes:
        cause = 'excludes'
    else:
        cause = 'seeds'
    assessment = {
        'regions': regions, 'expected': expected, 'split': split,
        'source': source, 'cells': cells, 'retained_cells': retained,
        'fragment_cells': (cells - retained
                           if cells is not None and retained is not None
                           else None),
        'largest_cells': sizes[0] if sizes else None,
        'single_cell_regions': (sum(1 for size in sizes if size == 1)
                                if sizes and len(sizes) == regions else None),
        'volume': getattr(result, 'total_volume', None),
        'estimate': dict(estimate) if estimate else None,
        'seeds': int(seeds or 0), 'excludes': excludes, 'cause': cause,
    }
    assessment['finding'] = finding_text(assessment) if split else ''
    assessment['message'] = message(assessment) if split else ''
    return assessment


def finding_text(assessment: dict) -> str:
    regions, expected = assessment['regions'], assessment['expected']
    return (f'{FINDING_PREFIX}{regions:,} disconnected regions '
            f'({expected:,} expected)')


def message(assessment: dict) -> str:
    """The plain sentences the summary, the evidence and the QA page carry."""
    regions, expected = assessment['regions'], assessment['expected']
    lines = [f'The mesh came out in {regions:,} disconnected regions where '
             f'{expected:,} {"was" if expected == 1 else "were"} expected.']
    cells, retained = assessment.get('cells'), assessment.get('retained_cells')
    if cells and retained is not None:
        share = 100.0 * retained / cells if cells else 0.0
        kept = ('The largest region holds' if expected == 1
                else f'The {expected:,} largest regions hold')
        lines.append(
            f'{kept} {count_text(retained, "cell")} of {cells:,} '
            f'({share:.0f}%); the other {regions - expected:,} hold '
            f'{count_text(cells - retained, "cell")}'
            + (f', {assessment["single_cell_regions"]:,} of them a single '
               f'cell' if assessment.get('single_cell_regions') else '')
            + '.')
    estimate = assessment.get('estimate') or {}
    volume = assessment.get('volume')
    if estimate.get('seeded_volume') and volume:
        removed = estimate.get('excluded_volume')
        lines.append(
            f'Before the run the seeded spaces measured about '
            f'{estimate["seeded_volume"]:.4g}'
            + (f' and the exclude points were to remove about {removed:.4g}'
               if removed else '')
            + f"; the mesh holds {volume:.4g}, in the model's units.")
    cause = assessment.get('cause')
    if cause == 'seeds_and_excludes':
        lines.append(
            'Exclude points set together with region seeds probably split '
            'it: OpenFOAM 13 then keeps every space without an exclude point '
            'and leaves cut-off pieces of the excluded space behind. Seed '
            'only the spaces to keep, delete the exclude points and mesh '
            'again.')
    elif cause == 'excludes':
        lines.append(
            'The exclude points probably split it: the space an exclude '
            'point removes can leave cut-off pieces behind where thin gaps '
            'cross the background cells. Seed the spaces to keep instead of '
            'excluding the others, or move each exclude point into the '
            'middle of the space it removes, and mesh again.')
    else:
        lines.append(
            'More pieces than region seeds usually means a thin passage was '
            'closed off during snapping or layer addition. Refine where the '
            'geometry narrows, check the region seeds and mesh again.')
    lines.append('FoamMesh does not remove the pieces itself.')
    return ' '.join(lines)


def apply(result, assessment: dict | None) -> None:
    """Attach *assessment* to *result*; a split mesh is not runnable.

    The finding is the app's judgement, not one of checkMesh's checks, so it
    goes to the blocking findings only -- ``failed_check_details`` stays what
    checkMesh itself reported (``empty_mesh_finding`` is filed the same way).
    """
    if not assessment:
        return
    result.retained = dict(assessment)
    if not assessment.get('split'):
        return
    finding = assessment['finding']
    if finding not in result.blocking_findings:
        result.blocking_findings.insert(0, finding)
    if result.incomplete:
        return
    result.runnable = False
    result.severity = 'fail'
    if assessment['message'] not in result.recommendations:
        result.recommendations.insert(0, assessment['message'])
    from .checkmesh_parser import _verdict

    result.verdict = _verdict(result)


def is_finding(text) -> bool:
    return str(text or '').startswith(FINDING_PREFIX)
