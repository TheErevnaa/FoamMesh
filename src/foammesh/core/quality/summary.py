"""The one document that binds three independent verdicts into a disposition.

Plan 23 §9. Fidelity, resolution and mesh quality are computed separately and
deliberately do not know about each other — §1's "three independent verdicts"
and §8.1's "complete diagnosis" both depend on that independence. Something
still has to say whether the mesh may leave, and this is it.

**It composes; it does not recompute.** Every block is a projection of a report
that already exists. A summary that re-derived a verdict could disagree with
the report it summarises, and then neither would be trustworthy.

**It refuses to compose over mismatched evidence.** If a block was computed
against a different mesh, prepared revision, policy or calculation version than
the case currently holds, the summary is `stale` rather than a blend of old and
new. §8.5's replay defence is the same rule at a different layer: evidence that
does not describe the present case is not weaker evidence, it is evidence about
something else.

**There is no path from here to green that a report did not earn.** A waiver
moves the disposition to `waived`, never to `qualified`. `unrated` — the state
the capability ships in, before WP8 calibrates thresholds — yields
`qualified: false` and blocks nothing, because report-only mode is the whole
point of shipping dark.

Two composition rules are less obvious and both come from §9:

*`mesh_quality` has two inputs that can disagree.* ``checkMesh`` is
authoritative, because the question the block asks is whether the solver can
run and ``checkMesh`` is the solver's own opinion. Canonical quality is
reported alongside and never merged. When they disagree the block is
``warning`` carrying both — silently preferring either would hide exactly the
signal this plan exists to show.

*snappy's GF1 is a blocking gate; Gmsh's is diagnostic.* A GF1 ``WARNING`` or
``WAIVED`` makes the disposition ``waived`` however well the final blocks
scored, because the mesh reached its final state through a gate somebody had to
open. On Gmsh the same report is optional and does not touch qualification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from .qualification import QualificationMode, qualification_mode

SUMMARY_SCHEMA_VERSION = 1

#: Where the summary lives, relative to the case. The export preflight reads
#: this exact path.
SUMMARY_PATH = Path('foammesh') / 'quality' / 'summary.json'

#: The three blocks of §9, and whether a non-pass gates a qualified export.
#: ``gmsh.fidelity_native`` is deliberately absent: it is diagnostic, so it is
#: carried in the document without being a block.
BLOCKS = ('geometry_fidelity', 'resolution_adequacy', 'mesh_quality')

#: Evidence every block must agree on. Same tuple the waiver binds to, because
#: they answer the same question -- "is this about the mesh in front of us?"
BOUND_EVIDENCE = ('checkpoint_fingerprint', 'subject_mesh_fingerprint',
                  'prepared_revision', 'policy_fingerprint',
                  'calculation_version')

#: Verdicts that do not block a qualified export.
PASSING = frozenset({'pass'})

#: Ordered worst-last, so composing is a max rather than a table of cases.
SEVERITY = ('pass', 'warning', 'unrated', 'incomplete', 'fail')

#: Blocking-gate states that opened the way to the final mesh without passing.
_GATE_WAIVED = frozenset({'WARNING', 'WAIVED'})
#: R144. Blocking-gate states that are outright failures. These had no entry
#: here at all, so a summary composed over ``state: FAIL`` fell through to
#: ``qualified: true`` whenever the three blocks happened to pass.
_GATE_FAILED = frozenset({'FAIL', 'FAILED', 'INVALID'})


def _rank(verdict: str) -> int:
    try:
        return SEVERITY.index(verdict)
    except ValueError:
        return SEVERITY.index('unrated')


@dataclass(frozen=True)
class Block:
    """One verdict, and the report it came from."""

    name: str
    verdict: str
    report_fingerprint: str = ''
    required: bool = True
    detail: Mapping = field(default_factory=dict)
    #: The workflow task that produced this block. §9 names blocks
    #: (``geometry_fidelity``) and §8.4 names tasks
    #: (``snappy.fidelity_final``); a waiver is recorded against the *task*.
    #: Carrying both is what lets a waiver be matched to a block at all --
    #: without it, coverage could only be "some waiver exists", which would
    #: let a waiver for resolution answer for a fidelity failure.
    task_id: str = ''

    def to_dict(self) -> dict:
        return {'verdict': self.verdict, 'required': self.required,
                'report_fingerprint': self.report_fingerprint,
                'task_id': self.task_id, 'detail': dict(self.detail)}


@dataclass(frozen=True)
class Summary:
    """§9's composed document."""

    blocks: Mapping[str, Block]
    evidence: Mapping[str, str]
    disposition: str
    qualified: bool
    mode: str
    stale: bool = False
    stale_reason: str = ''
    waiver_fingerprints: tuple[str, ...] = ()
    #: R119/R158. The waivers themselves, not just their content addresses.
    #: The document used to carry ``waiver_fingerprints`` alone, so the only
    #: thing a reviewer could read about a human overriding a failing gate was
    #: an opaque hash -- printed, in the measured case, on the same line as
    #: ``"waived": false``. Each entry keeps the overridden verdict, who
    #: decided, why, and whether it still describes this mesh.
    waivers: tuple[Mapping, ...] = ()
    blocking_gate_evidence: Mapping = field(default_factory=dict)
    diagnostics: Mapping = field(default_factory=dict)
    schema_version: int = SUMMARY_SCHEMA_VERSION

    @property
    def applied_waivers(self) -> tuple[Mapping, ...]:
        """The waivers that still describe the mesh this summary is about."""
        return tuple(item for item in self.waivers if item.get('applies'))

    @property
    def waived(self) -> bool:
        """Did a human override a gate to get this mesh to where it is?

        R119/R158: this used to be ``disposition == 'waived'`` alone, and the
        shipping mode is ``report_only`` -- whose disposition is *always*
        ``report_only``. So a mesh accepted through **Accept anyway** wrote
        ``"waived": false`` beside its own ``waiver_fingerprints`` list, and
        the document contradicted itself in the one field a reader would use.
        A waiver that covers this evidence is the fact; the disposition is a
        mode-dependent projection of it.
        """
        return self.disposition == 'waived' or bool(self.applied_waivers)

    @property
    def blocking_failures(self) -> tuple[Mapping, ...]:
        """Every recorded FAIL that a reader must not have to hunt for.

        R144: a live snappy run recorded ``blocking_gate_evidence:
        {task_id: snappy.fidelity_snap, state: FAIL}`` and ``geometry_fidelity:
        fail``, and the only thing the disposition said was ``report_only``.
        Nothing downstream had a single field to look at to learn that a
        blocking gate had failed, so Export unlocked without a word.
        """
        out = [{'kind': 'block', 'name': name, 'task_id': block.task_id,
                'verdict': block.verdict}
               for name, block in sorted(self.blocks.items())
               if block.required and block.verdict == 'fail']
        state = str(self.blocking_gate_evidence.get('state') or '').upper()
        if state in _GATE_FAILED:
            out.append({
                'kind': 'blocking_gate',
                'name': str(self.blocking_gate_evidence.get('task_id') or ''),
                'task_id': str(self.blocking_gate_evidence.get('task_id') or ''),
                'verdict': state.lower()})
        return tuple(out)

    @property
    def worst_verdict(self) -> str:
        """The most severe required verdict, for a one-glyph UI row.

        Severity order rather than a table of cases, and `fail` outranks
        `incomplete` deliberately: "we measured it and it failed" is a stronger
        statement than "we could not finish measuring", so the row must not
        soften a failure by pairing it with an unfinished block.
        """
        required = [block.verdict for block in self.blocks.values()
                    if block.required]
        return max(required, key=_rank) if required else 'unrated'

    @property
    def summary_fingerprint(self) -> str:
        return _digest(self._body())

    def _body(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'blocks': {name: block.to_dict()
                       for name, block in sorted(self.blocks.items())},
            'evidence': dict(sorted(self.evidence.items())),
            'disposition': self.disposition,
            'qualified': self.qualified,
            'qualification_mode': self.mode,
            'stale': self.stale,
            'stale_reason': self.stale_reason,
            'waiver_fingerprints': list(self.waiver_fingerprints),
            'waivers': [dict(item) for item in self.waivers],
            'blocking_gate_evidence': dict(self.blocking_gate_evidence),
            'diagnostics': dict(self.diagnostics),
        }

    def to_dict(self) -> dict:
        document = self._body()
        # Self-referential by construction: the fingerprint covers the body,
        # not itself, so re-reading and re-fingerprinting a written summary
        # reproduces the same value.
        document['summary_fingerprint'] = self.summary_fingerprint
        document['waived'] = self.waived
        document['worst_verdict'] = self.worst_verdict
        # R144. One field a reader can look at to learn that something failed,
        # whatever the mode-dependent disposition ended up saying.
        document['blocking_failures'] = [dict(item)
                                         for item in self.blocking_failures]
        return document


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')).hexdigest()


def mesh_quality_block(check_mesh: Mapping | None,
                       canonical: Mapping | None) -> Block:
    """§9's two-input rule. ``checkMesh`` decides; disagreement is surfaced.

    Neither input is recomputed here. ``checkMesh``'s verdict is the solver's
    own, and canonical quality's is FoamMesh's; the block's job is to say which
    one governs and to refuse to let the other vanish.
    """
    authoritative = str((check_mesh or {}).get('verdict') or '').lower()
    other = str((canonical or {}).get('verdict') or '').lower()

    if not authoritative:
        return Block('mesh_quality', 'incomplete',
                     detail={'reason': 'checkMesh has not run on this mesh',
                             'canonical_quality': other or 'not evaluated'})

    detail = {'check_mesh': authoritative,
              'canonical_quality': other or 'not evaluated'}
    fingerprint = str((check_mesh or {}).get('report_fingerprint') or '')

    if other and other != authoritative:
        # Reported, never resolved. A checkMesh-valid mesh that canonical
        # quality rejects is precisely the signal §9 refuses to discard.
        detail['disagreement'] = (
            f'checkMesh reports {authoritative}; canonical quality reports '
            f'{other}. checkMesh governs; both are recorded.')
        differing = (canonical or {}).get('differing_metric')
        if differing:
            detail['differing_metric'] = str(differing)
        return Block('mesh_quality', 'warning', fingerprint, detail=detail)

    return Block('mesh_quality', authoritative, fingerprint, detail=detail)


def compose(*, blocks: Sequence[Block], current_evidence: Mapping[str, str],
            block_evidence: Mapping[str, Mapping[str, str]] | None = None,
            blocking_gate: Mapping | None = None,
            waivers: Sequence = (), diagnostics: Mapping | None = None,
            mode: QualificationMode | None = None) -> Summary:
    """Build the summary, or mark it stale and explain which block disagreed.

    ``block_evidence`` maps a block name to the evidence *its report* was
    computed against. A block whose evidence differs from ``current_evidence``
    makes the whole summary stale: a document that quietly dropped the
    disagreeing block would report on a subset of the mesh while looking
    complete.
    """
    resolved = mode if mode is not None else qualification_mode()
    by_name = {block.name: block for block in blocks}
    evidence = {key: str(value) for key, value in current_evidence.items()}

    stale_reason = _stale_reason(by_name, evidence, block_evidence or {})
    gate = dict(blocking_gate or {})
    recorded = tuple(waivers)
    waiver_fingerprints = tuple(sorted(
        getattr(item, 'fingerprint', str(item)) for item in recorded))
    # R119/R158. Every waiver on the case is carried in full, each saying
    # whether it still describes this mesh. A lapsed waiver is kept rather
    # than dropped: "somebody decided this once and it no longer applies" is
    # itself part of the record.
    waiver_records = tuple(_waiver_record(item, evidence) for item in recorded)
    applied = tuple(item for item in recorded if _applies(item, evidence))

    if stale_reason:
        return Summary(
            blocks=by_name, evidence=evidence, disposition='unqualified',
            qualified=False, mode=resolved.value, stale=True,
            stale_reason=stale_reason, waiver_fingerprints=waiver_fingerprints,
            waivers=waiver_records, blocking_gate_evidence=gate,
            diagnostics=dict(diagnostics or {}))

    disposition, qualified = _disposition(
        by_name, gate, recorded, waiver_fingerprints, resolved, applied)
    return Summary(
        blocks=by_name, evidence=evidence, disposition=disposition,
        qualified=qualified, mode=resolved.value,
        waiver_fingerprints=waiver_fingerprints, waivers=waiver_records,
        blocking_gate_evidence=gate, diagnostics=dict(diagnostics or {}))


def _applies(waiver, evidence: Mapping[str, str]) -> bool:
    """Does *waiver* describe the mesh this summary is about?

    A caller-supplied stub with no ``covers`` is taken at its word; only a
    real :class:`~core.quality.waiver.Waiver` can answer the evidence bind.
    """
    covers = getattr(waiver, 'covers', None)
    if not callable(covers):
        return True
    try:
        return bool(covers(evidence))
    except (AttributeError, TypeError, ValueError):
        return False


def _waiver_record(waiver, evidence: Mapping[str, str]) -> dict:
    """One waiver as the summary carries it: the failure, not a hash."""
    return {
        'fingerprint': str(getattr(waiver, 'fingerprint', '') or ''),
        'task_id': str(getattr(waiver, 'task_id', '') or ''),
        'verdict': str(getattr(waiver, 'verdict', '') or ''),
        'actor': str(getattr(waiver, 'actor', '') or ''),
        'reason': str(getattr(waiver, 'reason', '') or ''),
        'report_fingerprint': str(
            getattr(waiver, 'report_fingerprint', '') or ''),
        'applies': _applies(waiver, evidence),
    }


def _stale_reason(by_name, evidence, block_evidence) -> str:
    missing = [key for key in BOUND_EVIDENCE if not evidence.get(key)]
    if missing:
        return (f'the case does not currently identify {", ".join(missing)}, '
                'so no block could be checked against it')
    for name in sorted(by_name):
        recorded = block_evidence.get(name)
        if recorded is None:
            continue
        for key in BOUND_EVIDENCE:
            mine, theirs = evidence.get(key), recorded.get(key)
            if theirs is not None and str(theirs) != str(mine):
                return (f'{name} was computed against {key}={theirs!r} but the '
                        f'case now holds {mine!r}; re-run it')
    return ''


def _disposition(by_name, gate, recorded, waiver_fingerprints, mode,
                 applied=()):
    """Which of §9's four dispositions applies, and is it qualified?"""
    required = [block for block in by_name.values() if block.required]
    non_pass = [block for block in required if block.verdict not in PASSING]

    # Report-only never claims qualification, whatever the blocks say. The
    # thresholds have not been calibrated, so a green here would be a claim
    # about a number nobody has justified.
    if not mode.enforces:
        return 'report_only', False

    gate_state = str(gate.get('state') or '').upper()
    gate_waived = gate_state in _GATE_WAIVED
    # R144. A blocking gate reading FAIL had no case at all here: it was
    # neither `gate_waived` nor a block, so a summary composed over
    # `blocking_gate_evidence: {state: FAIL}` returned `qualified: True` as
    # soon as the three blocks passed, and Export unlocked on it.
    gate_failed = gate_state in _GATE_FAILED
    gate_task = str(gate.get('task_id') or '')

    if applied and not non_pass and not gate_waived and not gate_failed:
        # R119/R158. Every block passed and a human still had to override a
        # gate to get here -- the engine's own element-quality gate, which §9
        # does not compose into a block. Claiming `qualified` was how the
        # document came to print `"waived": false` beside its own waiver list.
        return 'waived', False

    if not non_pass and not gate_waived and not gate_failed:
        return 'qualified', True

    covered = all(_is_covered(block, recorded) for block in non_pass)
    if gate_failed and not any(
            getattr(item, 'task_id', None) == gate_task for item in recorded):
        # A failed blocking gate nobody decided about is not waived; it is
        # simply unqualified, and saying so is the whole point of the gate.
        return 'unqualified', False
    if (covered and waiver_fingerprints) or (
            (gate_waived or gate_failed) and not non_pass):
        # Reached the end through a gate somebody opened. Never green.
        return 'waived', False
    return 'unqualified', False


def _is_covered(block, recorded) -> bool:
    """Is there a waiver recorded against *this block's* producing task?

    Matched on ``task_id``, not on "a waiver exists somewhere" -- otherwise a
    waiver accepting a resolution shortfall would silently answer for a
    fidelity failure, which is the one thing a per-block gate must not do. The
    *evidence* bind is still checked by the export preflight against the
    waiver's own recorded evidence; this is the narrower question of whether
    anybody decided about this block at all.
    """
    if not block.task_id:
        return False
    return any(getattr(item, 'task_id', None) == block.task_id
               for item in recorded)


def write(case_path: str | Path, summary: Summary) -> Path:
    """Persist the summary where the export preflight reads it."""
    path = Path(case_path) / SUMMARY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(
        json.dumps(summary.to_dict(), indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    os.replace(temporary, path)
    return path


def read(case_path: str | Path):
    """The stored summary document, or ``None`` when the case has none."""
    path = Path(case_path) / SUMMARY_PATH
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
