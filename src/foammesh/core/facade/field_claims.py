"""Expiring dirty-form leases that prevent conflicting automation edits.

A dirty GUI form acquires a named ``FieldClaimLease`` covering its field IDs.
Conflicting external mutations receive ``interaction_busy`` with the claim IDs
and remaining lease time.  Leases expire after an idle timeout unless the
owning form heartbeats them, so an abandoned dialog cannot wedge automation
indefinitely.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from uuid import uuid4

from .errors import InteractionBusyError

DEFAULT_LEASE_SECONDS = 30.0


@dataclass(frozen=True)
class FieldClaimLease:
    claim_id: str
    fields: tuple[str, ...]
    actor_id: str
    base_authored_revision: int | None
    acquired_at: float
    expires_at: float

    def remaining(self, now: float) -> float:
        return max(0.0, self.expires_at - now)

    def to_dict(self, *, now: float | None = None) -> dict:
        payload = {
            'claim_id': self.claim_id, 'fields': list(self.fields),
            'actor_id': self.actor_id,
            'base_authored_revision': self.base_authored_revision,
        }
        if now is not None:
            payload['remaining_seconds'] = round(self.remaining(now), 3)
        return payload


class FieldClaimRegistry:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._leases: dict[str, FieldClaimLease] = {}

    def claim(self, fields, *, actor_id: str, timeout: float = DEFAULT_LEASE_SECONDS,
              base_authored_revision: int | None = None) -> FieldClaimLease:
        if timeout <= 0:
            raise ValueError('claim timeout must be positive')
        fields = tuple(sorted(set(fields)))
        if not fields:
            raise ValueError('a claim must cover at least one field')
        self._expire()
        self._assert_conflict_free(fields, actor_id)
        now = self._clock()
        lease = FieldClaimLease(str(uuid4()), fields, actor_id, base_authored_revision,
                                now, now + timeout)
        self._leases[lease.claim_id] = lease
        return lease

    def heartbeat(self, claim_id: str, *, timeout: float = DEFAULT_LEASE_SECONDS) -> FieldClaimLease:
        """Renew a live lease from its owning form; expired leases stay expired."""
        self._expire()
        lease = self._leases.get(claim_id)
        if lease is None:
            raise KeyError(f'field claim does not exist or expired: {claim_id}')
        renewed = replace(lease, expires_at=self._clock() + timeout)
        self._leases[claim_id] = renewed
        return renewed

    def release(self, claim_id: str | None = None, *, actor_id: str | None = None) -> None:
        for key, lease in tuple(self._leases.items()):
            if claim_id is not None and key != claim_id:
                continue
            if actor_id is not None and lease.actor_id != actor_id:
                continue
            self._leases.pop(key, None)

    def assert_available(self, fields, *, actor_id: str) -> None:
        self._expire()
        self._assert_conflict_free(tuple(fields), actor_id)

    def active(self) -> tuple[FieldClaimLease, ...]:
        self._expire()
        return tuple(sorted(self._leases.values(), key=lambda lease: lease.acquired_at))

    def _assert_conflict_free(self, fields, actor_id: str) -> None:
        now = self._clock()
        conflicts = [lease for lease in self._leases.values()
                     if lease.actor_id != actor_id and set(lease.fields) & set(fields)]
        if conflicts:
            raise InteractionBusyError('field is being edited locally', details={
                'claims': [lease.to_dict(now=now) for lease in conflicts],
            })

    def _expire(self) -> None:
        now = self._clock()
        self._leases = {claim_id: lease for claim_id, lease in self._leases.items()
                        if lease.expires_at > now}
