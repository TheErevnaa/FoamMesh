"""Translate a stored ``checkMesh`` report into the GUI's verdict shape.

Two quality paths exist and they measure different things. Gmsh's gate
(:mod:`core.gmsh.quality`) judges *elements* against requested limits and can
name the offenders; ``checkMesh`` judges a *finished polyMesh* and reports
per-metric extrema plus the cell sets it wrote. WP3's strip and Mesh quality
tab were wired to the first only, so a mesh that was opened rather than
generated -- the entire external-mesh path -- had no verdict at all, and the
strip read "No mesh yet." over a mesh that was on screen.

This is the adapter. It lives in core rather than the view because the mapping
is a judgement, not a rendering: which severity becomes which word, and what
happens when checkMesh passes a mesh whose indicators are poor.

Three rules govern it:

* **The report's own severity decides runnability, indicators only worsen it.**
  ``checkMesh`` passing means the solver will start, which is not the same as
  the mesh being good. A mesh that passes with a ``poor`` indicator reads
  ``blemish``, never ``pass`` -- taking the better of the two is how a bad mesh
  comes to look fine, which WP8 already forbids for the report document.
* **An unread or incomplete check is ``unrated``, never ``pass``.** Absence of
  a finding is not a finding of absence.
* **Nothing here is overridable.** "Accept anyway" re-publishes a *generated*
  mesh with a recorded waiver; there is no run behind an opened mesh to accept,
  so the button stays disabled with that stated rather than offering an
  override that would bind to nothing.
"""
from __future__ import annotations

import re
from typing import Any

from foammesh.core.quantities import count_text

#: ``checkMesh`` severity -> the strip's vocabulary. ``incomplete`` means the
#: log was truncated or the run did not finish; it is not a pass.
SEVERITY_VERDICT = {
    'pass': 'pass',
    'warning': 'blemish',
    'fail': 'fail',
    'incomplete': 'unrated',
}

#: An indicator grade can only *worsen* the severity's verdict. ``poor`` maps
#: to ``blemish`` rather than ``fail`` deliberately: a mesh with a 300:1 aspect
#: ratio is bad, and it still runs. What makes a mesh unrunnable is a blocking
#: finding, and that already sets the severity to ``fail``.
GRADE_VERDICT = {'good': 'pass', 'marginal': 'blemish', 'poor': 'blemish',
                 'unknown': 'unrated'}

#: Worst-first, matching :data:`core.gmsh.quality.VERDICT_SEVERITY` for the
#: values they share. ``unrated`` sorts below ``pass``: "we did not measure" is
#: weaker evidence than "we measured and it was fine", so it must not win a
#: max() against a real result, but it must beat nothing at all.
_RANK = {'unrated': -1, 'pass': 0, 'blemish': 1, 'fail': 2, 'invalid': 3}


def _worst(*verdicts: str) -> str:
    known = [item for item in verdicts if item in _RANK]
    if not known:
        return 'unrated'
    return max(known, key=lambda item: _RANK[item])


#: DP-761. The names a verdict is shown under. The Gmsh element gate's
#: verdict (``core.gmsh.quality.QualityVerdict.to_dict``) carries no
#: ``source``; checkMesh's and the SU2 readiness check's do. One label,
#: ``Quality limits:``, used to stand for all three, so a Gmsh gate pass read
#: as a checkMesh pass over a table of unmeasured checkMesh metrics.
GMSH_GATE_SOURCE = 'Gmsh quality gate'
CHECKMESH_SOURCE = 'checkMesh'
SU2_READINESS_SOURCE = 'SU2 readiness check'


def verdict_source(verdict) -> str:
    """Who produced *verdict*, in the words the strip and the tab use."""
    return str((verdict or {}).get('source') or GMSH_GATE_SOURCE)


def _report_source(report: Any) -> str:
    command = tuple(getattr(report, 'command', ()) or ())
    return SU2_READINESS_SOURCE if command[:1] == ('su2-readiness',)         else CHECKMESH_SOURCE


def verdict_from_report(report: Any) -> dict:
    """Map a :class:`~core.quality.checkmesh_service.QualityReport` to a verdict.

    ``report`` may be ``None`` -- an unchecked mesh -- in which case the
    verdict is ``unrated`` and says which command would produce one, rather
    than leaving the surface blank for the user to interpret.
    """
    if report is None:
        return {
            'verdict': 'unrated',
            'reason': 'This mesh has not been checked. Run Mesh → Mesh '
                      'check to measure it.',
            'source': 'checkMesh',
            'metrics': [],
            'offending': [],
            'overridable': False,
            'stale': False,
        }

    result = report.result
    indicators = result.quality_indicators()
    severity = SEVERITY_VERDICT.get(str(result.severity or ''), 'unrated')
    grades = _worst(*(GRADE_VERDICT.get(str(item.get('grade')), 'unrated')
                      for item in indicators))
    if severity == 'unrated':
        # An incomplete or unparsed log. Whatever numbers were recovered came
        # from a run that did not finish, so grading them would let a truncated
        # check report a pass on the strength of the checks it got through --
        # which is the failure mode this whole adapter exists to avoid.
        verdict = 'unrated'
    else:
        # `unrated` indicators must not drag a real severity down to unrated,
        # so the two are combined by rank rather than by "whichever is worse".
        verdict = severity if grades == 'unrated' else _worst(severity, grades)

    names = failed_check_names(result)
    failed_checks = result.failed_checks
    if failed_checks is None and names:
        failed_checks = len(names)

    worst = result.worst_indicator
    measure = str(worst['name']) if worst else ''
    # No indicator is bad enough to name, yet checkMesh still had something to
    # say -- the elbow case is exactly this: every metric grades `good` and the
    # run is still a warning. Naming a metric here would point the user at a
    # number that is fine, so the mesh-wide grade stands in and the strip is
    # never reduced to a single bare word.
    # DP-137. `quality: good` here put the word "quality" on the grade scale
    # while the label in front of this very line -- `Quality limits:` -- put
    # it on the verdict scale. The grade is named for what it grades.
    detail = (_detail(worst) if worst
              else f'mesh grades {result.quality_grade}')

    out = {
        'verdict': verdict,
        'measure': measure,
        'detail': detail,
        'reason': _reason(result, verdict),
        'metrics': [_metric(item) for item in indicators],
        # checkMesh names cell *sets*, not element ids: it writes
        # `constant/polyMesh/sets/…` for the cells that failed. The tab's
        # element table is for the Gmsh gate; the sets are carried separately
        # so the viewport can still be pointed at them.
        'offending': [],
        'sets': [dict(item) for item in report.sets],
        'overridable': False,
        'source': _report_source(report),
        'checkedAt': str(report.checked_at or ''),
        'stale': bool(report.stale),
        'blocking': list(result.blocking_findings),
        'advisory': list(result.advisory_findings),
        # DP-542. checkMesh's own tally, carried as its own fact. Its absence
        # let a formally failed check read as `blemish · mesh grades
        # marginal` on the strip and as nothing at all on Export.
        'failedChecks': failed_checks,
        'failedCheckNames': names,
        'failedCheckLine': failed_check_line(failed_checks, names),
    }
    # Plan 37 F-2. A snappy mesh in more pieces than its seeds ask for is
    # carried as its own fact, with the sentence the QA page and the summary
    # say; only when it is split, so an ordinary verdict is unchanged.
    retained = getattr(result, 'retained', None)
    if isinstance(retained, dict) and retained.get('split'):
        out['retainedRegions'] = dict(retained)
    return out


def layer_shortfall_line(coverage) -> str:
    """``boundary layers not grown on elbow (3 requested)``, or ``''``.

    DP-664, MEASURED on mesh campaign 0925 S8: three layers were asked for
    on ``elbow``, snappy rolled every one of them back, and the layer record
    said so -- ``no prism layers were added across 1197 faces`` -- while the
    Export page read ``Mesh is runnable. Every metric grades good.`` checkMesh
    grades the cells that exist; a layer stage that grew nothing leaves no
    bad cell for it to find. So the shortfall is read from the layer record
    and said beside the grade.

    *coverage* is the ``mesh.layer_coverage`` payload. A patch that was not
    grown (below the coverage floor, or none at all) is a shortfall; a
    partial one is named as partial; a frozen one was a decision and a
    complete one is fine, so neither is mentioned.
    """
    from foammesh.core.quality.layer_report import coverage_rows

    missing, partial = [], []
    for row in coverage_rows(coverage):
        try:
            requested = float(row.get('requested') or 0)
        except (TypeError, ValueError):
            requested = 0.0
        if requested <= 0:
            continue
        if row['verdict'] == 'not grown':
            missing.append(row)
        elif row['verdict'] == 'partial':
            partial.append(row)
    parts = []
    if missing:
        parts.append('boundary layers not grown on ' + ', '.join(
            f"{row['patch']} ({row['requested_text']} requested)"
            for row in missing))
    if partial:
        parts.append('boundary layers partial on ' + ', '.join(
            f"{row['patch']} ({row['achieved_text']} of "
            f"{row['requested_text']})" for row in partial))
    return '; '.join(parts)


def apply_layer_coverage(verdict: dict, coverage) -> dict:
    """Carry a layer shortfall into the verdict the strip and Export show.

    DP-664. A patch whose requested layers were not grown worsens a ``pass``
    to ``blemish`` -- the mesh still runs, it is not the mesh that was asked
    for -- and the line is added to the headline and carried as
    ``layerLine`` for the strip and the Export page. A partial patch is
    named without changing the word. An unrated verdict stays unrated: there
    is no measured mesh for the shortfall to qualify.
    """
    line = layer_shortfall_line(coverage)
    if not verdict or not line:
        return verdict
    result = dict(verdict)
    result['layerLine'] = line
    if 'not grown' in line and result.get('verdict') == 'pass':
        result['verdict'] = 'blemish'
    reason = str(result.get('reason') or '').rstrip()
    sentence = line[0].upper() + line[1:] + '.'
    if 'not grown' in line:
        # DP-782. The shortfall with nothing to change read as a verdict on
        # the user's geometry; the measured cause and the controls go with it.
        from foammesh.core.quality.layer_report import NOT_GROWN_REMEDY
        sentence += ' ' + NOT_GROWN_REMEDY
    sentence += ' See the Quality step.'
    result['reason'] = f'{reason} {sentence}'.strip()
    return result


def failed_check_names(result: Any) -> list[str]:
    """Short names of the checks checkMesh itself counted as failed.

    DP-542, MEASURED on S5 (venturi) and S6 (two cubes), 24 Sep 2026: both
    logs end ``Failed 1 mesh checks.`` over ``***Concave cells (using face
    planes) found, number of cells: 1151`` (536 on S6). The strip read
    ``Quality limits: blemish · mesh grades marginal`` and the Export page
    said nothing, so a formally failed check reached the handoff reading like
    a clean pass. The finding classifies advisory -- the mesh does run -- and
    that stays; what was lost is that checkMesh *counted* it.

    ``Concave cells (using face planes) found, …`` names ``concave cells``:
    the text before the first parenthesis, ``found``, ``detected`` or
    punctuation. The size findings DP-450 adds are the app's judgement, not a
    checkMesh check, and are left out of the names.
    """
    from foammesh.core.quality.checkmesh_parser import empty_mesh_finding

    ours = empty_mesh_finding(result)
    names = []
    for detail in getattr(result, 'failed_check_details', None) or ():
        if not detail or detail == ours:
            continue
        text = re.sub(r'^[*\s]+', '', str(detail))
        head = re.split(r'\s*\(|\s+(?:found|detected)|[:,=]', text, 1)[0]
        name = head.strip().rstrip('.').lower()
        if name and name not in names:
            names.append(name)
    return names


def failed_check_line(count, names=()) -> str:
    """``1 checkMesh check failed: concave cells``, or ``''`` for none.

    One spelling for the strip, the Mesh quality tab and the Export page, so
    the same tally is never said two ways.
    """
    if not count:
        return ''
    line = f'{count_text(count, "checkMesh check")} failed'
    names = [str(item) for item in (names or ()) if item]
    return f'{line}: {", ".join(names)}' if names else line


def apply_waivers(verdict: dict, waivers) -> dict:
    """Mark a verdict that a human had to override to get this far.

    R119/R158. ``checkMesh`` and the generator's own element gate measure
    different things, and a mesh can pass the first while failing the second.
    On the measured run it did exactly that: **Accept anyway** recorded a
    waiver over a ``sicn`` failure (1,107 of 90,418 elements below 0.1) and the
    strip then re-read ``checkMesh`` and painted ``Quality limits: pass ·
    quality: good``. Nothing on screen said a gate had been overridden.

    The verdict word is *not* rewritten -- ``checkMesh`` really did pass, and
    inventing a failure it did not report would be the same class of lie in
    the other direction. What is added is the decision itself, so a surface
    can show the flag beside the word.

    ``waivers`` is any sequence of waiver records: :class:`Waiver` instances or
    the plain dicts the facade and ``summary.json`` carry.
    """
    records = [_waiver_fields(item) for item in (waivers or ())]
    records = [item for item in records if item['verdict']]
    if not records:
        return dict(verdict or {})
    out = dict(verdict or {})
    out['waived'] = True
    out['waivers'] = records
    # The worst overridden verdict is what the flag has to say: a surface
    # showing "waived" without saying waived *from what* has moved the
    # failure out of sight again rather than into view.
    out['waived_verdict'] = max(
        (item['verdict'] for item in records),
        key=lambda word: _RANK.get(_WAIVED_VERDICT.get(word, word), 0))
    out['waiver_reason'] = records[0]['reason']
    out['waiver_actor'] = records[0]['actor']
    return out


#: A waiver spells the gate's severities the way §8.6 does; the strip's
#: vocabulary is the one in :data:`_RANK`.
_WAIVED_VERDICT = {'warning': 'blemish', 'incomplete': 'unrated'}


def _waiver_fields(waiver) -> dict:
    reader = (waiver.get if isinstance(waiver, dict)
              else lambda key, default=None: getattr(waiver, key, default))
    return {
        'task_id': str(reader('task_id', '') or ''),
        'verdict': str(reader('verdict', '') or ''),
        'actor': str(reader('actor', '') or ''),
        'reason': str(reader('reason', '') or ''),
        'fingerprint': str(reader('fingerprint', '') or ''),
    }


def _metric(indicator: dict) -> dict:
    """One indicator as a Mesh quality table row.

    ``checkMesh`` reports extrema, not counts, so ``belowThreshold`` and
    ``total`` stay ``None`` and the table renders them as unmeasured. Filling
    them with zeros would read as "nothing failed", which is a different claim
    from "this was never counted".
    """
    return {
        'measure': str(indicator.get('name') or ''),
        'requestedMinimum': str(indicator.get('acceptable') or ''),
        'achievedMinimum': indicator.get('critical'),
        'achievedMean': indicator.get('average'),
        'criticalKind': str(indicator.get('critical_kind') or ''),
        'units': str(indicator.get('units') or ''),
        'belowThreshold': None,
        'total': None,
        'allowance': None,
        'verdict': GRADE_VERDICT.get(str(indicator.get('grade')), 'unrated'),
        'grade': str(indicator.get('grade') or ''),
        'reason': str(indicator.get('note') or ''),
    }


def _detail(indicator: dict) -> str:
    """``max 66.13 deg`` -- the figure behind the word, or nothing."""
    critical = indicator.get('critical')
    if critical is None:
        return ''
    units = str(indicator.get('units') or '')
    kind = str(indicator.get('critical_kind') or '')
    return f'{kind} {critical:g}{" " + units if units else ""}'.strip()


def _reason(result: Any, verdict: str) -> str:
    """The parser's own prose where it has any; a composed line otherwise."""
    if result.verdict:
        return str(result.verdict)
    if verdict == 'unrated':
        return 'The checkMesh log was incomplete, so this mesh is unrated.'
    if result.blocking_findings:
        return '; '.join(str(item) for item in result.blocking_findings[:3])
    if result.advisory_findings:
        return '; '.join(str(item) for item in result.advisory_findings[:3])
    return 'checkMesh found nothing to report.'
