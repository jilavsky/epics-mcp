"""Rate limiting and confirm tokens for the write path (PLAN.md 4.5-4.6).

Both are stateful, per-process concerns and deliberately kept out of
`policy.py`, which stays pure (load a file, answer "is this allowed" with no
memory of past calls). `server.py` calls here only after `Policy.can_write`
has already said yes.
"""

from __future__ import annotations

import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class TokenBucket:
    """A sliding 60-second window, not a classic leaky bucket.

    "N per minute" is expressed as: at most N timestamps recorded in the
    trailing 60 seconds. Simpler than a true token bucket and exactly what
    `rate_limit_per_min` / `max_writes_per_min` mean in the policy file.
    """

    def __init__(self, capacity_per_min: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = capacity_per_min
        self._clock = clock
        self._timestamps: deque[float] = deque()

    def _evict_expired(self) -> None:
        cutoff = self._clock() - 60.0
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def would_allow(self) -> bool:
        self._evict_expired()
        return len(self._timestamps) < self.capacity

    def consume(self) -> None:
        self._timestamps.append(self._clock())


class RateLimiter:
    """Per-write-rule buckets plus one policy-wide bucket.

    Checking never consumes on its own: `check_and_consume` peeks both
    buckets first and only records the call in either if both would allow
    it, so a write blocked by its own rule's limit does not also spend a
    slot in the shared ceiling.
    """

    def __init__(
        self,
        *,
        max_writes_per_min: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._global = TokenBucket(max_writes_per_min, clock=clock) if max_writes_per_min else None
        self._per_rule: dict[str, TokenBucket] = {}

    def check_and_consume(self, rule_pattern: str, rule_limit: int | None) -> tuple[bool, str | None]:
        rule_bucket = None
        if rule_limit is not None:
            rule_bucket = self._per_rule.setdefault(
                rule_pattern, TokenBucket(rule_limit, clock=self._clock)
            )
            if not rule_bucket.would_allow():
                return False, f"rate limit exceeded for rule {rule_pattern!r} ({rule_limit}/min)"

        if self._global is not None and not self._global.would_allow():
            return False, "global write rate limit exceeded (max_writes_per_min)"

        if rule_bucket is not None:
            rule_bucket.consume()
        if self._global is not None:
            self._global.consume()
        return True, None


@dataclass(frozen=True)
class ConfirmChallenge:
    token: str
    pv: str
    value: Any
    session: str | None
    expires_at: float


class ConfirmTokenStore:
    """Single-use, short-lived tokens for `writes:` rules with `confirm: true`.

    A token is bound to the exact `(pv, value, session)` triple it was
    issued for (PLAN.md 4.6) and is consumed by the *attempt* to redeem it,
    whether or not that attempt matches -- a mismatched redemption burns the
    token rather than leaving it available for guessing.
    """

    def __init__(self, *, ttl_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._pending: dict[str, ConfirmChallenge] = {}

    def issue(self, pv: str, value: Any, *, session: str | None = None) -> ConfirmChallenge:
        token = secrets.token_hex(8)
        challenge = ConfirmChallenge(
            token=token, pv=pv, value=value, session=session, expires_at=self._clock() + self.ttl_s
        )
        self._pending[token] = challenge
        return challenge

    def redeem(
        self, token: str, *, pv: str, value: Any, session: str | None = None
    ) -> tuple[bool, str | None]:
        challenge = self._pending.pop(token, None)
        if challenge is None:
            return False, "unknown or already-used confirm token"
        if self._clock() > challenge.expires_at:
            return False, "confirm token expired"
        if challenge.pv != pv or challenge.value != value:
            return False, "confirm token does not match this pv/value"
        if challenge.session != session:
            return False, "confirm token was issued to a different session"
        return True, None

    def purge_expired(self) -> None:
        now = self._clock()
        expired = [t for t, c in self._pending.items() if now > c.expires_at]
        for t in expired:
            del self._pending[t]
