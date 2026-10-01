"""Turn a stored quality report into the few lines a page can put on screen.

Plan 23 §9 requires the same evidence to be visible from the GUI, the CLI and
the API, and until now only the file had it: ``fidelity.json``,
``resolution.json`` and ``summary.json`` were written, read by the export
preflight, and rendered nowhere (R31, R41, R68, R90, R103, R122, R123, R160).
The pages that should have shown them were blank grey panels.

This module is the projection those surfaces share. It is pure and Qt-free for
the same reason :mod:`core.quality.verdict` is: choosing which number is worth
a row, and which absence has to be stated out loud, is a judgement about the
evidence rather than a rendering detail -- and a judgement made inside a widget
can be tested by nobody and reused by nothing.

Two rules run through all of it.

**Nothing here upgrades a verdict.** The headline repeats what the report
computed. A readout that summarised four ``incomplete`` sections as "checked"
would be the defect these rows describe, one layer up.

**An unmeasured check says so in its own sentence.** ``measured`` counts the
sections that produced a number, separately from ``rated``, because a report
can rate every section and measure none of them -- which is exactly what R99,
R121 and R161 found on disk (``rated: 4``, four sections, not one distance
computed). A caveat naming that is the difference between "the mesh follows
the geometry" and "we never asked".
"""
from __future__ import annotations

import functools

from dataclasses import dataclass
from typing import Mapping

#: Worst-last, matching :data:`core.quality.summary.SEVERITY`. Spelled out here
#: rather than imported so a readout cannot drag the summary module -- and its
#: file IO -- into a widget's import graph.
SEVERITY = ('pass', 'warning', 'unrated', 'incomplete', 'fail')


def _rank(verdict: str) -> int:
    try:
        return SEVERITY.index(str(verdict))
    except ValueError:
        return SEVERITY.index('unrated')


def _number(value, spec: str = '.4g') -> str:
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return '-'


@dataclass(frozen=True)
class ReadoutRow:
    """One named line of evidence: what it is, how it rated, what was seen."""

    name: str
    verdict: str
    detail: str = ''


@dataclass(frozen=True)
class Readout:
    """A whole report, reduced to a headline, some rows and a caveat."""

    kind: str
    verdict: str
    headline: str
    rows: tuple[ReadoutRow, ...] = ()
    #: Stated when the numbers do not support the verdict they produced --
    #: never used to soften a bad verdict, only to refuse to imply a good one.
    caveat: str = ''
    measured: int = 0
    total: int = 0

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'verdict': self.verdict,
                'headline': self.headline, 'caveat': self.caveat,
                'measured': self.measured, 'total': self.total,
                'rows': [{'name': row.name, 'verdict': row.verdict,
                          'detail': row.detail} for row in self.rows]}


def absent(kind: str, what: str) -> Readout:
    """The readout for a check that has not run.

    ``unrated``, never ``pass``: absence of a finding is not a finding of
    absence, and a page that showed nothing at all for this case is what let a
    user settle a gate that had measured nothing (R90).
    """
    return Readout(
        kind, 'unrated',
        f'Not run. {what} has produced no report for this case yet, so there '
        'is nothing measured to show.')


def _exceeds_tolerance(section: Mapping) -> bool:
    """Whether a section deviated past the tolerance resolved for it.

    R183. Independent of the threshold set: `ratio` is the deviation over the
    tolerance the project stated, so 1.0 is the line the user drew, and saying
    it was crossed claims nothing about calibration.
    """
    try:
        return float(section.get('ratio')) > 1.0
    except (TypeError, ValueError):
        return False


def fidelity_readout(document: Mapping | None,
                     what: str = 'Geometry fidelity') -> Readout:
    """Project a ``fidelity.json`` / ``fidelity-snap.json`` document.

    The per-section verdict *and* the reason behind it. R39 and R99 found five
    of six sections reading "measurement did not complete inside the diagnostic
    budget" in the file while the page said nothing at all; on this evidence
    the reason *is* the finding, so it is the row.
    """
    if not document:
        return absent('fidelity', what)
    sections = list(document.get('sections') or ())
    rows, measured = [], 0
    for section in sections:
        metrics = dict(section.get('metrics') or {})
        deviation = section.get('deviation')
        if deviation is None:
            detail = str(section.get('reason') or metrics.get('reason')
                         or 'no measurement recorded')
        else:
            measured += 1
            tolerance = _number(section.get('tolerance'))
            detail = (f'worst deviation {_number(deviation)} m against a '
                      f'{tolerance} m tolerance')
            source = str(section.get('tolerance_source') or '')
            if source:
                detail += f' ({source})'
            ratio = section.get('ratio')
            if ratio is not None:
                detail += f', {_number(ratio, ".2f")}x'
            if section.get('reason'):
                detail += '. ' + str(section['reason'])
        rows.append(ReadoutRow(str(section.get('name') or '(unnamed)'),
                               str(section.get('verdict') or 'unrated'),
                               detail))
    verdict = str(document.get('verdict') or 'unrated')
    total = len(sections)
    rated = sum(1 for section in sections
                if str(section.get('verdict') or 'unrated') != 'unrated')
    # R183. Deviation past a tolerance the user themselves set is arithmetic,
    # not a threshold judgement, so it is reportable while nothing rates.
    exceeded = tuple(str(section.get('name') or '(unnamed)')
                     for section in sections
                     if _exceeds_tolerance(section))
    headline = (f'{verdict} — {measured} of {total} sections were measured '
                'against the reference.')
    caveat = ''
    if total and not measured:
        caveat = ('No section was measured against a reference, so this '
                  'verdict describes the check, not the mesh. Nothing here '
                  'says the mesh follows the geometry.')
    elif measured < total:
        caveat = (f'{total - measured} of {total} sections produced no '
                  'measurement; their rows say why.')
    elif total and not rated:
        # Every section measured and not one rated is a different absence
        # from a section that produced no number, and it had no caveat at
        # all: the live page read `unrated - 4 of 4 sections were measured`
        # over a boundary 32% past its stated tolerance and said nothing
        # about either fact.
        caveat = ('Every section was measured and none was rated: the '
                  'thresholds that turn a deviation into a pass or a fail '
                  'have not been calibrated, so these rows are readings '
                  'rather than verdicts.')
    if not total:
        headline = f'{verdict} — the report contains no sections.'
        caveat = ('Nothing was reconciled against the prepared geometry, so '
                  'this is a report about the check rather than the mesh.')
    if exceeded:
        caveat = (caveat + ' ' if caveat else '') + (
            f'{len(exceeded)} of {total} sections deviate by more than the '
            f'tolerance set for them: {", ".join(exceeded)}.')
    return Readout('fidelity', verdict, headline, tuple(rows), caveat,
                   measured, total)


def resolution_readout(document: Mapping | None) -> Readout:
    """Project a ``resolution.json`` document.

    R122: this file holds the most useful numbers in the case -- cells across
    the gap, probe counts, achieved-against-requested edge length -- and every
    one of them stopped at the disk. They are the rows.
    """
    if not document:
        return absent('resolution', 'Resolution adequacy')
    sections = list(document.get('sections') or ())
    rows, measured = [], 0
    for section in sections:
        metrics = dict(section.get('metrics') or {})
        resolution = dict(metrics.get('resolution') or {})
        probes = dict(metrics.get('probes') or {})
        parts = []
        for channel in resolution.get('channels') or ():
            if str(channel.get('status') or '') == 'measured':
                measured += 1
                required = channel.get('required')
                parts.append(
                    f'{int(channel.get("minimum") or 0)} cells across at '
                    f'worst, median {_number(channel.get("median"), ".1f")}, '
                    f'over a {_number(channel.get("gap_mean"))} m mean gap'
                    + (f'; {int(required)} required' if required
                       else '; nothing requires a minimum'))
            else:
                parts.append(str(channel.get('reason')
                                 or f'channel {channel.get("status")}'))
        size = dict(resolution.get('size') or {})
        if size:
            parts.append(
                f'mean boundary edge {_number(size.get("achieved_mean"))} is '
                f'{_number(size.get("ratio"), ".2f")}x the requested '
                f'{_number(size.get("requested"))}')
        if probes:
            parts.append(f'{int(probes.get("paired") or 0)} of '
                         f'{int(probes.get("cast") or 0)} probes paired')
        if section.get('reason'):
            parts.append(str(section['reason']))
        rows.append(ReadoutRow(str(section.get('name') or '(unnamed)'),
                               str(section.get('verdict') or 'unrated'),
                               '; '.join(part for part in parts if part)
                               or 'no measurement recorded'))
    verdict = str(document.get('verdict') or 'unrated')
    total = len(sections)
    headline = (f'{verdict} — {measured} channel'
                f'{"" if measured == 1 else "s"} traversed across {total} '
                f'section{"" if total == 1 else "s"}.')
    caveat = ''
    if total and all(str(item.get('verdict')) == 'unrated'
                     for item in sections):
        caveat = ('Every channel was measured and none was gated: no '
                  'min_cells_across policy applies to these sections, so the '
                  'counts below are readings, not a pass.')
    elif not total:
        headline = f'{verdict} — the report contains no sections.'
        caveat = ('No section could be joined to a prepared patch, so nothing '
                  'was traversed.')
    return Readout('resolution', verdict, headline, tuple(rows), caveat,
                   measured, total)


def summary_readout(document: Mapping | None) -> Readout:
    """Project a ``summary.json`` document.

    R38 and R161: the composed disposition reached the disk and never the
    screen, while the interface showed a tick and unlocked Export. The headline
    leads with the disposition and whether it is qualified, in that order,
    because ``report_only`` with ``qualified: false`` is the shipping default
    and reads as a pass to anyone shown neither word.
    """
    if not document:
        return absent('summary', 'The qualification summary')
    blocks = dict(document.get('blocks') or {})
    rows = []
    for name in sorted(blocks):
        block = dict(blocks[name] or {})
        detail = dict(block.get('detail') or {})
        parts = []
        for key in ('reason', 'disagreement', 'check_mesh',
                    'canonical_quality'):
            if detail.get(key):
                parts.append(f'{key.replace("_", " ")}: {detail[key]}')
        if not block.get('required', True):
            parts.append('diagnostic only, does not gate qualification')
        rows.append(ReadoutRow(name.replace('_', ' '),
                               str(block.get('verdict') or 'unrated'),
                               '; '.join(parts)))
    disposition = str(document.get('disposition') or 'unqualified')
    qualified = bool(document.get('qualified'))
    worst = str(document.get('worst_verdict') or 'unrated')
    headline = (f'{disposition} — '
                f'{"qualified" if qualified else "not qualified"}; worst '
                f'block verdict {worst}.')
    caveats = []
    if document.get('stale'):
        caveats.append('Stale: ' + str(
            document.get('stale_reason')
            or 'a block was computed against a different mesh.'))
    if disposition == 'report_only':
        caveats.append(
            'Report-only: the thresholds have not been calibrated, so this '
            'run is reported on and never qualified. Nothing below is a pass '
            'the mesh earned.')
    elif not qualified:
        caveats.append(
            'This mesh is not qualified. Exporting it publishes a mesh the '
            "app's own record says did not pass.")
    gate = dict(document.get('blocking_gate_evidence') or {})
    state = str(gate.get('state') or '').upper()
    if state and state != 'PASS':
        caveats.append(
            f'The blocking gate {gate.get("task_id") or "GF1"} is {state}.')
    # R38/R144. The one field a reader can look at to learn that something
    # failed, whatever the mode-dependent disposition ended up saying. It is
    # named here because `report_only` is the shipping default and swallows
    # the failure everywhere else.
    failures = list(document.get('blocking_failures') or ())
    if failures:
        named = ', '.join(
            f'{item.get("name") or item.get("task_id")} '
            f'({item.get("verdict") or item.get("state")})'
            for item in failures)
        caveats.append(f'Recorded failures: {named}.')
    if document.get('waived'):
        caveats.append('A human waiver was applied; waived is not a pass.')
    return Readout('summary', worst, headline, tuple(rows),
                   ' '.join(caveats), len(rows), len(rows))


def checkmesh_readout(document: Mapping | None) -> Readout:
    """Project a stored ``checkMesh`` report for the QA page.

    R100: the run reported ``Failed 1 mesh checks`` and 1,451 concave cells,
    the row went to warning, and no word of it appeared on the page, in the
    strip or in the console. Every finding checkMesh recorded gets a row.
    """
    if not document:
        return absent('checkmesh', 'checkMesh')
    result = dict(document.get('result') or {})
    rows = []
    for finding in result.get('blocking_findings') or ():
        rows.append(ReadoutRow('blocking', 'fail', str(finding)))
    for finding in result.get('advisory_findings') or ():
        rows.append(ReadoutRow('advisory', 'warning', str(finding)))
    if not rows:
        # Older reports carry the raw warning lines and no classified
        # findings; dropping those would show an empty panel beside a
        # `warning` row, which is the defect this fixes.
        for finding in result.get('warnings') or ():
            rows.append(ReadoutRow('warning', 'warning', str(finding)))
    for label, key, spec in (('max non-orthogonality', 'max_non_ortho', '.4g'),
                             ('max skewness', 'max_skewness', '.4g'),
                             ('max aspect ratio', 'max_aspect_ratio', '.4g'),
                             ('min determinant', 'min_determinant', '.4g'),
                             ('cells', 'cells', '.0f')):
        if result.get(key) is not None:
            rows.append(ReadoutRow(
                label, 'pass' if result.get('mesh_ok') else 'unrated',
                _number(result[key], spec)))
    # Plan 37 F-2. A snappy mesh in more pieces than its seeds ask for: the
    # count beside the metrics, and the sentence -- cause and what to do --
    # as the caveat, where the page shows it in the warning style.
    retained = result.get('retained')
    split = isinstance(retained, dict) and bool(retained.get('split'))
    if split:
        rows.append(ReadoutRow(
            'regions', 'fail',
            f"{int(retained['regions']):,} "
            f"({int(retained.get('expected') or 1):,} expected)"))
    severity = str(result.get('severity') or 'unrated')
    failed = int(result.get('failed_checks') or 0)
    headline = (f'{severity} — {failed} failed mesh check'
                f'{"" if failed == 1 else "s"}, '
                f'{len(result.get("blocking_findings") or ())} blocking and '
                f'{len(result.get("advisory_findings") or ())} advisory '
                'findings.')
    caveat = ''
    if document.get('stale'):
        caveat = ('This report was written against a different mesh than the '
                  'one the case holds now; re-run the check.')
    elif result.get('incomplete'):
        caveat = ('The checkMesh log was truncated, so this is an incomplete '
                  'reading rather than a pass.')
    elif split:
        caveat = str(retained.get('message') or '')
    return Readout('checkmesh', severity, headline, tuple(rows), caveat,
                   len(rows), len(rows))


#: Which projection a qualification task's report needs, keyed on the task id
#: the page already carries -- so a page cannot pick the wrong one by hand.
# R201. All three fidelity tasks shared one projection, and its not-run
# sentence names its subject in prose: "Geometry fidelity has produced no
# report for this case yet". MEASURED on tee / gmsh, that sentence appeared
# under the heading "Native mesh fidelity", telling a user a different check
# had not run and sending them to look at a task that had already passed.
# One projection still, bound to the name each page shows.
READOUTS = {
    'common.fidelity': fidelity_readout,
    'snappy.fidelity_snap': functools.partial(
        fidelity_readout, what='Snap fidelity'),
    'gmsh.fidelity_native': functools.partial(
        fidelity_readout, what='Native mesh fidelity'),
    'common.resolution': resolution_readout,
    'common.summary': summary_readout,
    # R208. `checkmesh_readout` was written for the QA page and never
    # registered, so `quality.evidence.read` refused the one task id the QA
    # page carries -- the CLI and the API could not read a checkMesh verdict
    # at all, and Gmsh's QA page showed an empty rectangle above the word
    # "Accepted.". Both engines run the same check and write the same file.
    'gmsh.qa': checkmesh_readout,
    'snappy.qa': checkmesh_readout,
}


def readout_for(task_id: str, document: Mapping | None) -> Readout:
    """The readout for *task_id*'s stored report."""
    projection = READOUTS.get(str(task_id))
    if projection is None:
        raise KeyError(f'no readout for task {task_id!r}')
    return projection(document)


__all__ = ['READOUTS', 'Readout', 'ReadoutRow', 'SEVERITY', 'absent',
           'checkmesh_readout', 'fidelity_readout', 'readout_for',
           'resolution_readout', 'summary_readout']
