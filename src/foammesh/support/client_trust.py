"""Which programs may drive FoamMesh over its local API.

The token in the discovery file proves only that a program runs as this user
on this PC. Any program that does can read it. So the token buys a handshake
and nothing else: the API sends a single-use challenge, the client signs it
with a private key whose public half FoamMesh trusts, and only then gets the
session token every other route needs.

FoamMesh ships public keys only. The private keys stay with whoever runs the
client, so a stock build answers only the clients those keys belong to.
"""
from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass, field

#: The text a client signs is this, a newline, then the challenge.
CONTEXT = b'foammesh-local-api-handshake-v1'

#: Key id -> raw Ed25519 public key, base64.
TRUSTED_CLIENT_KEYS = {
    'erevnaa-2026-10': 'usRqtHPxQuFCVzPs4MGlro+TJGaudkCcLCREEQAvP/E=',
}

CHALLENGE_SECONDS = 60.0


def signed_message(challenge: str) -> bytes:
    """What the client signs for ``challenge``."""
    return CONTEXT + b'\n' + challenge.encode('ascii')


@dataclass
class ClientTrust:
    """Challenges handed out, the keys that may answer them, and the sessions won."""

    keys: dict[str, str]
    clock: object = time.monotonic
    _challenges: dict[str, float] = field(default_factory=dict)
    _sessions: set[str] = field(default_factory=set)

    @classmethod
    def erevnaa(cls) -> 'ClientTrust':
        return cls(dict(TRUSTED_CLIENT_KEYS))

    def challenge(self) -> dict:
        now = self.clock()
        # Forget the ones nobody answered.
        self._challenges = {c: t for c, t in self._challenges.items() if t > now}
        value = secrets.token_urlsafe(32)
        self._challenges[value] = now + CHALLENGE_SECONDS
        return {'challenge': value, 'expires_in': CHALLENGE_SECONDS,
                'key_ids': sorted(self.keys), 'signs': CONTEXT.decode() + '\\n<challenge>',
                'algorithm': 'Ed25519'}

    def answer(self, challenge: str, key_id: str, signature: str) -> str | None:
        """A session token when ``signature`` is a trusted key's, else None.

        A challenge is spent by its first answer, right or wrong.
        """
        expires = self._challenges.pop(str(challenge), None)
        if expires is None or expires <= self.clock():
            return None
        public = self.keys.get(str(key_id))
        if public is None:
            return None
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public))
            key.verify(base64.b64decode(signature, validate=True), signed_message(challenge))
        except (InvalidSignature, ValueError, TypeError):
            return None
        session = secrets.token_urlsafe(32)
        self._sessions.add(session)
        return session

    def session_valid(self, session: str | None) -> bool:
        return session is not None and any(
            secrets.compare_digest(session, held) for held in self._sessions)
