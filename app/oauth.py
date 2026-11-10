"""Pure pieces of the connect flow: pending requests, scope checks, token age.

No network and no database here, so every rule is easy to test.
"""

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.db.models import ROLES
from app.providers.oauth import READ_SCOPE, pkce_challenge

FLOW_TTL = timedelta(minutes=10)
MAX_PENDING = 20

# Testing-mode refresh tokens expire after 7 days (M0 item 4)
TOKEN_LIFETIME = timedelta(days=7)
WARN_BEFORE = timedelta(days=2)

TokenStatus = Literal["ok", "expiring", "expired", "needs_reauth"]


@dataclass(frozen=True)
class PendingFlow:
    verifier: str
    role: str
    created_at: datetime


@dataclass(frozen=True)
class FlowStart:
    state: str
    challenge: str


class PendingFlows:
    """Authorization requests waiting for Google's callback.

    The callback cannot carry the login cookie (it is SameSite=Strict and the
    request comes from Google), so the state is the proof: it is random, only
    a signed-in user can create one, it lasts 10 minutes, and it works once.
    """

    def __init__(self, ttl: timedelta = FLOW_TTL, limit: int = MAX_PENDING) -> None:
        self.ttl = ttl
        self.limit = limit
        self._flows: dict[str, PendingFlow] = {}
        self._lock = threading.Lock()

    def _prune(self, now: datetime) -> None:
        expired = [s for s, f in self._flows.items() if now - f.created_at >= self.ttl]
        for state in expired:
            del self._flows[state]

    def start(self, role: str, now: datetime) -> FlowStart:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        state = secrets.token_urlsafe(32)
        # RFC 7636: 43 to 128 characters from the unreserved set
        verifier = secrets.token_urlsafe(64)
        with self._lock:
            self._prune(now)
            if len(self._flows) >= self.limit:
                oldest = min(self._flows, key=lambda s: self._flows[s].created_at)
                del self._flows[oldest]
            self._flows[state] = PendingFlow(verifier, role, now)
        return FlowStart(state, pkce_challenge(verifier))

    def finish(self, state: str, now: datetime) -> PendingFlow | None:
        """Remove and return the flow for this state, or None if unknown or expired."""
        with self._lock:
            self._prune(now)
            return self._flows.pop(state, None)


def scopes_ok(granted: frozenset[str]) -> bool:
    """Accept exactly the read scope: nothing missing, nothing extra."""
    return granted == frozenset({READ_SCOPE})


def expires_at(authorized_at: datetime) -> datetime:
    return authorized_at + TOKEN_LIFETIME


def token_status(authorized_at: datetime, needs_reauth: bool, now: datetime) -> TokenStatus:
    if needs_reauth:
        return "needs_reauth"
    left = expires_at(authorized_at) - now
    if left <= timedelta(0):
        return "expired"
    if left <= WARN_BEFORE:
        return "expiring"
    return "ok"
