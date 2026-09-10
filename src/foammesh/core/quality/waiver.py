"""The engineer's recorded decision to proceed on a mesh that did not pass.

Plan 23 §8.6. A waiver is the one route to :data:`TaskState.WAIVED`, and its
whole purpose is to be *narrower* than the decision it records. Three
properties do that work:

**It is bound to evidence, not to a task name.** A waiver names the report it
overrides and the checkpoint, subject mesh, prepared revision, policy and
calculation version that report was computed against. Re-mesh the case and the
waiver stops covering it -- not because anything revokes it, but because it no
longer describes the mesh in front of you. A waiver that survived re-meshing
would be worse than no waiver, because it would look like a decision someone
made about *this* mesh.

**It cannot manufacture a pass.** It never touches the report's verdict, and
the state it produces is its own. §9's summary reads ``WAIVED`` as
``qualified: false`` with ``disposition: waived``. There is deliberately no
path from here to green.

**It refuses to record a decision nobody needed to make.** A passing report
cannot be waived. That is not pedantry: a waiver over a pass is a decision
with no subject, and a file full of them makes the ones that matter
unfindable.

Actor identity comes from the caller's session context and not from a
user-supplied display string, so the record says who the system believed was
acting rather than what they typed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Mapping

WAIVER_SCHEMA_VERSION = 1

#: Where waivers live, relative to the case.
WAIVER_DIRECTORY = Path('foammesh') / 'quality' / 'waivers'

#: Verdicts a waiver may override. A waiver exists to carry a decision past a
#: report that did not pass; ``pass`` is not such a report. ``unrated`` and
#: ``incomplete`` are here because §8.6 treats "we could not measure this" as
#: an engineering decision in exactly the way "we measured it and it failed"
#: is -- both need a name attached.
OVERRIDABLE_VERDICTS = frozenset({'warning', 'fail', 'unrated', 'incomplete'})

#: The evidence keys a waiver binds itself to. Adding a key here narrows every
#: future waiver; removing one silently widens every existing one, which is why
#: the tuple is checked against the report rather than inferred from it.
BOUND_EVIDENCE = ('checkpoint_fingerprint', 'subject_mesh_fingerprint',
                  'prepared_revision', 'policy_fingerprint',
                  'calculation_version')


class WaiverRefused(RuntimeError):
    """A waiver was requested that §8.6 does not permit.

    Carries a stable ``reason`` token so a caller can distinguish "this cannot
    be waived" from "this did not need waiving" without parsing prose.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Waiver:
    """One immutable acceptance decision."""

    task_id: str
    report_fingerprint: str
    verdict: str
    actor: str
    reason: str
    evidence: Mapping[str, str] = field(default_factory=dict)
    schema_version: int = WAIVER_SCHEMA_VERSION

    @property
    def fingerprint(self) -> str:
        """Content address. Two identical decisions are one file."""
        return _digest(self.to_dict())

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'task_id': self.task_id,
            'report_fingerprint': self.report_fingerprint,
            'verdict': self.verdict,
            'actor': self.actor,
            'reason': self.reason,
            'evidence': dict(sorted(self.evidence.items())),
        }

    def covers(self, evidence: Mapping[str, str]) -> bool:
        """Does this waiver describe the mesh currently in front of us?

        Every bound key must match. A key absent from *either* side is a
        mismatch rather than a wildcard: the permissive reading would let a
        waiver recorded against a case that had no prepared revision cover a
        later case that does.
        """
        for key in BOUND_EVIDENCE:
            # R158. A blank is treated exactly like an absent key. A live
            # waiver was recorded with ``prepared_revision: ""`` while
            # ``evidence.json`` for the same run held ``pg-1445135f6c5e21a8``,
            # and an empty string compares equal to another empty string --
            # so a waiver that named no geometry would have covered every
            # case that also named none.
            mine = str(self.evidence.get(key) or '').strip()
            theirs = str(evidence.get(key) or '').strip()
            if not mine or not theirs or mine != theirs:
                return False
        return True


def _digest(payload: Mapping) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')).hexdigest()


def record(task, report: Mapping, *, actor: str, reason: str) -> Waiver:
    """Build the waiver for *report*, or refuse and say which rule refused.

    ``task`` is the :class:`WorkflowTask`; ``report`` the stored report
    document. Nothing is written here -- :func:`write` does that -- so a
    refusal cannot leave a partial record behind.
    """
    if not getattr(task, 'accepts_override', False):
        raise WaiverRefused(
            f'{task.task_id} does not accept an override; a task grants this '
            'by declaring accepts_override, which the qualification gates and '
            "Gmsh's mesh-quality gate do and no other task does",
            reason='not_overridable')

    text = (reason or '').strip()
    if not text:
        raise WaiverRefused(
            'a waiver requires a stated reason; an unexplained override is '
            'indistinguishable from an accident',
            reason='no_reason')

    who = (actor or '').strip()
    if not who:
        raise WaiverRefused(
            'a waiver requires an actor from the session context',
            reason='no_actor')

    verdict = str(report.get('verdict') or '').strip().lower()
    if verdict not in OVERRIDABLE_VERDICTS:
        raise WaiverRefused(
            f'a {verdict or "missing"} verdict cannot be waived; waivers '
            f'record a decision to proceed despite '
            f'{sorted(OVERRIDABLE_VERDICTS)}',
            reason='not_overridable_verdict')

    fingerprint = str(report.get('report_fingerprint') or '').strip()
    if not fingerprint:
        raise WaiverRefused(
            'the report carries no fingerprint, so a waiver could not be '
            'bound to it', reason='unfingerprinted_report')

    # R158. Present-but-blank is not identification. The Gmsh gate wrote
    # ``prepared_revision: ""`` into its report -- the call site read the
    # revision off the wrong object -- and the waiver recorded that empty
    # string as though it named the geometry the decision was granted
    # against, which is the one link an auditor follows back.
    evidence = {key: str(report[key]).strip() for key in BOUND_EVIDENCE
                if str(report.get(key) or '').strip()}
    missing = [key for key in BOUND_EVIDENCE if key not in evidence]
    if missing:
        raise WaiverRefused(
            f'the report does not identify {", ".join(missing)}, so a waiver '
            'could not state which mesh it covers',
            reason='unbound_evidence')

    return Waiver(task_id=task.task_id, report_fingerprint=fingerprint,
                  verdict=verdict, actor=who, reason=text, evidence=evidence)


def write(case_path: str | Path, waiver: Waiver) -> Path:
    """Persist *waiver* under its own fingerprint, immutably.

    An existing file is left alone rather than rewritten. The name is the
    content address, so a file that is already there has the same content by
    construction; rewriting it would only risk truncating a record another
    process is reading.
    """
    directory = Path(case_path) / WAIVER_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{waiver.fingerprint}.json'
    if path.exists():
        return path
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(
        json.dumps(waiver.to_dict(), indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    os.replace(temporary, path)
    return path


def read(path: str | Path) -> Waiver:
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    version = int(document.get('schema_version') or 0)
    if version != WAIVER_SCHEMA_VERSION:
        raise WaiverRefused(
            f'waiver schema {version} is not {WAIVER_SCHEMA_VERSION}',
            reason='schema_version')
    return Waiver(
        task_id=str(document.get('task_id') or ''),
        report_fingerprint=str(document.get('report_fingerprint') or ''),
        verdict=str(document.get('verdict') or ''),
        actor=str(document.get('actor') or ''),
        reason=str(document.get('reason') or ''),
        evidence=dict(document.get('evidence') or {}),
        schema_version=version)


def load_all(case_path: str | Path) -> tuple[Waiver, ...]:
    """Every waiver on the case, newest-first by nothing in particular.

    Unreadable files are skipped rather than raising. A corrupt waiver must not
    be able to block export authorization *by existing*, because the failure
    mode of that is a case nobody can ship and no way to see why.
    """
    directory = Path(case_path) / WAIVER_DIRECTORY
    if not directory.is_dir():
        return ()
    out = []
    for path in sorted(directory.glob('*.json')):
        try:
            out.append(read(path))
        except (OSError, ValueError, WaiverRefused):
            continue
    return tuple(out)


def covering(waivers: Iterable[Waiver], task_id: str,
             evidence: Mapping[str, str]) -> Waiver | None:
    """The waiver that covers *task_id* against current *evidence*, if any."""
    for waiver in waivers:
        if waiver.task_id == task_id and waiver.covers(evidence):
            return waiver
    return None
