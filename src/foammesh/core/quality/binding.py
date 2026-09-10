"""Which candidate a quality report and a waiver actually belong to.

Plan 31 CP-05 item 2. Waivers were already bound to evidence -- §8.6 makes a
waiver name the checkpoint, subject mesh, prepared revision, policy and
calculation version it was granted against, and
:meth:`core.quality.waiver.Waiver.covers` refuses a mesh that does not match
all five. What did not exist was any way to put *one run's* evidence in front
of that check.

``plans/evidence/plan31/FAULT_REGISTER.md``, DP-06: "checkMesh and fidelity
reports carry a *task* id rather than a run id. **Binding run identity into
those reports is CP-05's.**" MEASURED: ``grep -rn 'run_id|artifact_id'
src/foammesh/core/quality/`` matched nothing at all, and a case holds exactly
one ``foammesh/quality/mesh-quality.json`` that every run rewrites. So after
a second candidate was refused and waived, inspecting the first candidate was
served the second one's report and the second one's waiver -- a decision
displayed against a mesh nobody took it about.

This module answers two questions per run, and only from that run's own
record:

* which report judged this run's artifact;
* which waivers, if any, were granted against *that* report.

It reads run manifests as plain JSON rather than through
:mod:`core.gmsh.manifest`, for the reason :mod:`core.run_result` does: the
quality package must not depend on an engine package.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import waiver as waiver_module
from .waiver import BOUND_EVIDENCE, Waiver

#: The file each run writes describing itself.
RUN_MANIFEST = 'run-manifest.json'

#: Where a run's own copy of its quality report is kept on the manifest.
RUN_REPORT_KEY = 'quality_report'

#: Case-level quality reports a run's report can be recovered from when the
#: manifest carries no copy of its own -- a run recorded before the copy
#: existed. Each entry is a path relative to the case and the field in that
#: document which names the mesh it judged; the report is claimed for a run
#: only when that field is the run's own ``mesh_sha256``.
CASE_REPORTS = (
    (Path('foammesh') / 'quality' / 'mesh-quality.json',
     'subject_mesh_fingerprint'),
)


def run_manifest(case_path: str | Path, run_id: str) -> dict:
    """One run's manifest document, or ``{}`` if there is not one."""
    if not str(run_id or '').strip():
        return {}
    path = (Path(case_path) / 'foammesh' / 'runs' / str(run_id)
            / RUN_MANIFEST)
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _recovered_report(case_path: Path, mesh_fingerprint: str) -> dict | None:
    """A case-level report, but only when it names *this* mesh."""
    if not mesh_fingerprint:
        return None
    for relative, field in CASE_REPORTS:
        try:
            document = json.loads(
                (case_path / relative).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        if str(document.get(field) or '').strip() == mesh_fingerprint:
            return document
    return None


def run_report(case_path: str | Path, run_id: str) -> dict | None:
    """The quality report that judged *this run's* artifact, or ``None``.

    The run's own copy first. Failing that, the one report the case holds --
    claimed only when it names this run's mesh, so a run the current report
    does not describe is honestly uncovered rather than served somebody
    else's numbers.
    """
    document = run_manifest(case_path, run_id)
    if not document:
        return None
    stored = document.get(RUN_REPORT_KEY)
    if isinstance(stored, dict) and stored:
        return dict(stored)
    return _recovered_report(
        Path(case_path), str(document.get('mesh_sha256') or '').strip())


def run_evidence(case_path: str | Path, run_id: str) -> dict:
    """The five bound-evidence keys for one run, from its own report."""
    report = run_report(case_path, run_id)
    if not report:
        return {}
    return {key: str(report.get(key) or '').strip()
            for key in BOUND_EVIDENCE}


def waivers_for_run(case_path: str | Path, run_id: str, *,
                    task_id: str = '') -> tuple[Waiver, ...]:
    """Every waiver granted against *this run's* report.

    Three things must agree: the task the decision was taken on, the report it
    named, and the five pieces of evidence that report stated. A waiver on a
    different candidate fails the second and third; a waiver that survived a
    re-mesh fails the third, which is the property §8.6 exists to give it.
    """
    report = run_report(case_path, run_id)
    if not report:
        return ()
    fingerprint = str(report.get('report_fingerprint') or '').strip()
    wanted = str(task_id or report.get('task_id') or '').strip()
    evidence = {key: str(report.get(key) or '').strip()
                for key in BOUND_EVIDENCE}
    found = []
    for item in waiver_module.load_all(case_path):
        if wanted and str(item.task_id or '') != wanted:
            continue
        if fingerprint and str(item.report_fingerprint or '').strip() != fingerprint:
            continue
        if not item.covers(evidence):
            continue
        found.append(item)
    return tuple(found)


def run_quality(case_path: str | Path, run_id: str, *,
                task_id: str = '') -> dict:
    """One candidate's quality evidence, as a surface would show it.

    ``bound`` says whether anything was found to bind to: a run with no
    report of its own and no case report that names its mesh is reported as
    unbound rather than as a run with a clean sheet.
    """
    report = run_report(case_path, run_id)
    waivers = waivers_for_run(case_path, run_id, task_id=task_id)
    return {
        'run_id': str(run_id or ''),
        'bound': report is not None,
        'report': report,
        'waivers': [dict(item.to_dict(), fingerprint=item.fingerprint)
                    for item in waivers],
    }
