"""Tests for rate limiting and confirm tokens (PLAN.md 4.5-4.6)."""

from __future__ import annotations

from epics_mcp.ratelimit import ConfirmTokenStore, RateLimiter, TokenBucket


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_token_bucket_allows_up_to_capacity():
    clock = FakeClock()
    bucket = TokenBucket(3, clock=clock)
    for _ in range(3):
        assert bucket.would_allow()
        bucket.consume()
    assert bucket.would_allow() is False


def test_token_bucket_recovers_after_window_slides():
    clock = FakeClock()
    bucket = TokenBucket(1, clock=clock)
    assert bucket.would_allow()
    bucket.consume()
    assert bucket.would_allow() is False
    clock.advance(61.0)
    assert bucket.would_allow() is True


def test_rate_limiter_per_rule_limit():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    ok, _ = limiter.check_and_consume("usx:foo", rule_limit=2)
    assert ok
    ok, _ = limiter.check_and_consume("usx:foo", rule_limit=2)
    assert ok
    ok, reason = limiter.check_and_consume("usx:foo", rule_limit=2)
    assert not ok
    assert "rate limit exceeded for rule" in reason


def test_rate_limiter_different_rules_have_independent_buckets():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    ok, _ = limiter.check_and_consume("usx:foo", rule_limit=1)
    assert ok
    ok, _ = limiter.check_and_consume("usx:bar", rule_limit=1)
    assert ok  # different rule, independent bucket


def test_rate_limiter_global_ceiling():
    clock = FakeClock()
    limiter = RateLimiter(max_writes_per_min=2, clock=clock)
    ok, _ = limiter.check_and_consume("usx:foo", rule_limit=None)
    assert ok
    ok, _ = limiter.check_and_consume("usx:bar", rule_limit=None)
    assert ok
    ok, reason = limiter.check_and_consume("usx:baz", rule_limit=None)
    assert not ok
    assert "global write rate limit" in reason


def test_rate_limiter_does_not_consume_global_slot_on_rule_failure():
    """A write blocked by its own rule's limit must not also spend a slot
    in the shared ceiling -- PLAN.md's `check_and_consume` peeks before
    consuming either bucket."""
    clock = FakeClock()
    limiter = RateLimiter(max_writes_per_min=10, clock=clock)
    ok, _ = limiter.check_and_consume("usx:foo", rule_limit=1)
    assert ok
    ok, reason = limiter.check_and_consume("usx:foo", rule_limit=1)
    assert not ok
    assert "rule" in reason
    # The global bucket should still have room for a different rule.
    ok, _ = limiter.check_and_consume("usx:bar", rule_limit=None)
    assert ok


def test_confirm_token_round_trip():
    store = ConfirmTokenStore()
    challenge = store.issue("usx:foo", 5.0, session="abc")
    ok, reason = store.redeem(challenge.token, pv="usx:foo", value=5.0, session="abc")
    assert ok
    assert reason is None


def test_confirm_token_is_single_use():
    store = ConfirmTokenStore()
    challenge = store.issue("usx:foo", 5.0, session="abc")
    store.redeem(challenge.token, pv="usx:foo", value=5.0, session="abc")
    ok, reason = store.redeem(challenge.token, pv="usx:foo", value=5.0, session="abc")
    assert not ok
    assert "unknown" in reason


def test_confirm_token_wrong_value_is_rejected_and_burns_the_token():
    store = ConfirmTokenStore()
    challenge = store.issue("usx:foo", 5.0, session="abc")
    ok, reason = store.redeem(challenge.token, pv="usx:foo", value=999.0, session="abc")
    assert not ok
    assert "does not match" in reason
    # Burned even though it mismatched -- prevents brute-forcing the value.
    ok, _ = store.redeem(challenge.token, pv="usx:foo", value=5.0, session="abc")
    assert not ok


def test_confirm_token_wrong_pv_is_rejected():
    store = ConfirmTokenStore()
    challenge = store.issue("usx:foo", 5.0, session="abc")
    ok, reason = store.redeem(challenge.token, pv="usx:other", value=5.0, session="abc")
    assert not ok
    assert "does not match" in reason


def test_confirm_token_wrong_session_is_rejected():
    store = ConfirmTokenStore()
    challenge = store.issue("usx:foo", 5.0, session="abc")
    ok, reason = store.redeem(challenge.token, pv="usx:foo", value=5.0, session="different")
    assert not ok
    assert "different session" in reason


def test_confirm_token_expires():
    clock = FakeClock()
    store = ConfirmTokenStore(ttl_s=60.0, clock=clock)
    challenge = store.issue("usx:foo", 5.0, session="abc")
    clock.advance(61.0)
    ok, reason = store.redeem(challenge.token, pv="usx:foo", value=5.0, session="abc")
    assert not ok
    assert "expired" in reason


def test_purge_expired_removes_only_expired_tokens():
    clock = FakeClock()
    store = ConfirmTokenStore(ttl_s=60.0, clock=clock)
    old = store.issue("usx:old", 1.0)
    clock.advance(61.0)
    fresh = store.issue("usx:fresh", 2.0)
    store.purge_expired()
    assert old.token not in store._pending
    assert fresh.token in store._pending
