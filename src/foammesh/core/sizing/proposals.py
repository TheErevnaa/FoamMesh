"""The proposal store ``sizing/proposals.json`` (Plan 41 4.8.2, section 12.2 S2).

One entry per target control: the proposed value, whether the control now
holds the field's value or one the user typed, and whether it is locked.

The rule that matters: **a proposal never overwrites a value the user typed.**
A control is the field's while it is empty or still holds exactly the value
the last Apply wrote; any other value in it was typed (or edited) by the user,
which the store notices at the next Apply and remembers.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import math
import os
from pathlib import Path

SCHEMA_VERSION = 1
SOURCE_FIELD = 'field'
SOURCE_USER = 'user'
SKIP_LOCKED = 'locked'
SKIP_TYPED = 'typed'
#: The control already holds the proposed value: nothing to write.
SKIP_UNCHANGED = 'unchanged'
#: The control belongs to a stage that already ran upstream of the page
#: applying: it is offered on its own page (the badge), not written here.
SKIP_STAGE_RAN = 'stage_ran'


def proposals_path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'sizing' / 'proposals.json'


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _same(left, right) -> bool:
    a, b = _number(left), _number(right)
    if a is not None and b is not None:
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-15)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)) \
            and not isinstance(left, bool) and not isinstance(right, bool):
        return math.isclose(float(left), float(right), rel_tol=1e-9,
                            abs_tol=1e-15)
    return left == right


@dataclass
class Proposal:
    target: str
    value: object
    rule: str = ''
    badge: str = ''
    source: str = SOURCE_FIELD
    locked: bool = False
    #: What the last Apply wrote into the control; ``None`` before one did.
    applied: object = None

    def to_dict(self) -> dict:
        return asdict(self)


class ProposalStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.field_digest = ''
        #: What the last Compute could not propose, said on the card (GN25f).
        self.notes: list[str] = []
        self.entries: dict[str, Proposal] = {}

    # -- persistence ------------------------------------------------------- #

    @classmethod
    def load(cls, path) -> 'ProposalStore':
        store = cls(path)
        try:
            document = json.loads(Path(path).read_text('utf-8'))
        except (OSError, ValueError):
            return store
        store.field_digest = str(document.get('field_digest') or '')
        store.notes = [str(n) for n in document.get('notes') or ()]
        for row in document.get('entries', ()):
            names = Proposal.__dataclass_fields__
            entry = Proposal(**{k: v for k, v in row.items() if k in names})
            store.entries[entry.target] = entry
        return store

    def to_dict(self) -> dict:
        return {'schema_version': SCHEMA_VERSION,
                'field_digest': self.field_digest, 'notes': self.notes,
                'entries': [item.to_dict() for item in self.entries.values()]}

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.to_dict(), indent=1), 'utf-8')
        os.replace(temporary, self.path)

    # -- proposing --------------------------------------------------------- #

    def propose(self, proposals, field_digest='') -> list[str]:
        """Record *proposals* (``Proposal`` or ``(target, value, rule, badge)``).

        Re-derive after a field change: only field-sourced, unlocked entries
        take the new value. Returns the targets whose proposal changed.
        """
        changed = []
        self.field_digest = field_digest or self.field_digest
        for item in proposals:
            if not isinstance(item, Proposal):
                item = Proposal(*item)
            old = self.entries.get(item.target)
            if old is None:
                self.entries[item.target] = item
                changed.append(item.target)
                continue
            if old.locked or old.source == SOURCE_USER:
                old.badge = item.badge or old.badge
                continue
            if not _same(old.value, item.value):
                changed.append(item.target)
            old.value, old.rule, old.badge = item.value, item.rule, item.badge
        return changed

    def lock(self, target: str, locked: bool = True) -> bool:
        entry = self.entries.get(target)
        if entry is None:
            return False
        entry.locked = bool(locked)
        return True

    def mark_user(self, target: str) -> None:
        """The user typed into the control: the field no longer owns it."""
        entry = self.entries.get(target)
        if entry is not None:
            entry.source = SOURCE_USER

    # -- applying ---------------------------------------------------------- #

    def apply(self, current: dict, targets=None, defaults=None
              ) -> tuple[dict, dict]:
        """``(writes, skipped)`` for the controls whose values are *current*.

        ``writes`` maps target -> value to put in the control: exactly the
        empty or field-sourced ones. ``skipped`` maps target -> reason.
        A stored control always holds something, so one still holding its
        schema default (``defaults``) counts as empty: nobody typed it.
        """
        defaults = defaults or {}
        writes, skipped = {}, {}
        for target, entry in self.entries.items():
            if targets is not None and target not in targets:
                continue
            if entry.locked:
                skipped[target] = SKIP_LOCKED
                continue
            held = current.get(target)
            untouched = (defaults.get(target) is not None
                         and _same(held, defaults[target]))
            field_owned = _empty(held) or untouched or (
                entry.source == SOURCE_FIELD and entry.applied is not None
                and _same(held, entry.applied))
            if not field_owned:
                entry.source = SOURCE_USER
                skipped[target] = SKIP_TYPED
                continue
            entry.source = SOURCE_FIELD
            entry.applied = entry.value
            if _same(held, entry.value):
                # FIX-H. The control already holds the proposal. Writing it
                # again changed only its stored form ("150.0" became "150")
                # and that was enough to stale a stage that had run on it.
                skipped[target] = SKIP_UNCHANGED
                continue
            writes[target] = entry.value
        return writes, skipped

    def badges(self) -> dict:
        return {target: entry.badge for target, entry in self.entries.items()
                if entry.badge and entry.source == SOURCE_FIELD}
