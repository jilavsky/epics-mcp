"""FastMCP tool surface for epics-mcp (PLAN.md 3).

Tools are prefixed ``epics_`` (matching ``pyirena_*``, so multiple servers
stay unambiguous to a client and to a small LLM) and are registered against
the module-level ``mcp`` server. :func:`configure` must be called once
before the server runs or its tools are called; it wires in the policy, the
CA backend, the audit log, the rate limiter and the confirm-token store, and
-- the part that matters -- conditionally registers ``epics_pv_put`` only
when the effective policy allows writes at all. A tool the client cannot
see is a stronger guarantee than a tool that refuses (PLAN.md 3).

**Exception vs. structured-JSON convention** (PLAN.md 3): a PV the policy
will never allow for this call, regardless of retries -- denied, wrong
mode, no matching write rule, value outside a rule's bound, a malformed
argument -- raises. An instrument-side condition (disconnected, slow) or a
step in the write protocol that a retry *can* resolve (confirm required, an
invalid/expired token, a rate limit) comes back as a JSON status instead,
because the model should not treat those like a hard wall.

**Session identity for v1**: confirm tokens and audit records are not yet
bound to a specific client connection. This is fine today because the
stdio transport this server defaults to is inherently one process per
client (AIDA spawns a subprocess per connection); it stops being fine once
the streamable-HTTP transport (PLAN.md 6 phase 7) serves multiple
concurrent clients from one process, at which point confirm-token binding
needs a real per-connection id from the MCP ``Context``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from epics_mcp.audit import AuditLog
from epics_mcp.ca_client import CaBackend, PyepicsBackend
from epics_mcp.catalog import CatalogEntry
from epics_mcp.catalog import load_catalog as load_catalog_file
from epics_mcp.catalog import search as search_catalog
from epics_mcp.policy import Policy
from epics_mcp.ratelimit import ConfirmTokenStore, RateLimiter

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "epics-mcp",
    instructions=(
        "Policy-gated EPICS Channel Access. All tool names are prefixed "
        "'epics_'. Call epics_policy_describe() first to see what this "
        "server is allowed to read and (if at all) write -- a denial is a "
        "policy boundary, not an instrument problem, and retrying the same "
        "call will not change it. epics_pv_search() looks up PV names by "
        "keyword against a static catalog; Channel Access itself has no "
        "name search. epics_pv_get() never raises for a disconnected or "
        "slow PV -- that entry comes back as {'connected': false, "
        "'error': ...} instead. epics_pv_put() exists only when this "
        "server's policy allows writes at all; if you cannot see it in "
        "your tool list, this deployment is read-only by design."
    ),
)


class PolicyDenied(Exception):
    """A PV/operation the effective policy will never allow for this call.

    Distinct from an instrument-side condition (PLAN.md 3): retrying the
    same call will not help. Dropping the offending PV, or fixing the
    value/mode, is the only way forward.
    """


@dataclass
class _State:
    policy: Policy
    backend: CaBackend
    audit: AuditLog | None
    limiter: RateLimiter
    confirm_store: ConfirmTokenStore
    catalog: tuple[CatalogEntry, ...]
    client_label: str


_state: _State | None = None


def _require_state() -> _State:
    if _state is None:
        raise RuntimeError(
            "epics_mcp.server.configure() was not called -- see cli.py or "
            "call server.configure(policy, backend=...) yourself."
        )
    return _state


def configure(
    policy: Policy,
    *,
    backend: CaBackend | None = None,
    client_label: str = "epics-mcp",
) -> None:
    """Wire the tool surface to ``policy``.

    Safe to call more than once (tests do): re-evaluates whether
    ``epics_pv_put`` should be registered rather than leaving a stale
    registration from a previous policy in place.
    """
    global _state

    resolved_backend = backend if backend is not None else PyepicsBackend()

    audit_log = None
    if policy.audit is not None:
        audit_log = AuditLog(
            policy.audit.path, max_bytes=policy.audit.max_bytes, keep=policy.audit.keep
        )

    catalog_entries: tuple[CatalogEntry, ...] = ()
    if policy.catalog_path is not None and policy.catalog_path.is_file():
        catalog_entries = load_catalog_file(policy.catalog_path)
    elif policy.catalog_path is not None:
        logger.warning("policy names catalog %s but it does not exist", policy.catalog_path)

    _state = _State(
        policy=policy,
        backend=resolved_backend,
        audit=audit_log,
        limiter=RateLimiter(max_writes_per_min=policy.max_writes_per_min),
        confirm_store=ConfirmTokenStore(),
        catalog=catalog_entries,
        client_label=client_label,
    )

    try:
        mcp.remove_tool(epics_pv_put.__name__)
    except ToolError:
        pass
    if policy.writes_enabled:
        mcp.add_tool(epics_pv_put)

    for warning in policy.warnings:
        logger.warning("policy warning: %s", warning)


# ---------------------------------------------------------------------------
# audit helpers
# ---------------------------------------------------------------------------


def _audit(event: str, **fields: Any) -> None:
    state = _require_state()
    if state.audit is None:
        return
    state.audit.record(
        event,
        policy=str(state.policy.source_path),
        policy_sha256=state.policy.sha256,
        client=state.client_label,
        **fields,
    )


def _audit_read_decision(pv: str, allowed: bool, reason: str) -> None:
    state = _require_state()
    if state.audit is None:
        return
    audit_cfg = state.policy.audit
    if allowed and not audit_cfg.log_reads:
        return
    if not allowed and not audit_cfg.log_denials:
        return
    _audit("read", pv=pv, allowed=allowed, reason=reason)


def _check_read_or_raise(name: str) -> None:
    state = _require_state()
    decision = state.policy.can_read(name)
    _audit_read_decision(name, decision.allowed, decision.reason)
    if not decision.allowed:
        raise PolicyDenied(f"{name!r} is not readable under this policy: {decision.reason}")


# ---------------------------------------------------------------------------
# epics_pv_get
# ---------------------------------------------------------------------------


@mcp.tool()
def epics_pv_get(names: list[str]) -> list[dict]:
    """Read one or more PVs.

    Returns one reading per name, in order:
    {pv, value, units, connected, timestamp, severity, status, error}.
    A disconnected or slow PV comes back as {"connected": false,
    "error": "..."} -- never an exception -- so one dead IOC does not fail
    a batch of otherwise-fine reads.

    Every name must be allowed by the server's policy; if unsure what that
    covers, call epics_policy_describe() first. Any denied name raises
    immediately, naming which one(s) and why -- drop them and retry with
    the rest, retrying unchanged will not help.
    """
    state = _require_state()
    if len(names) > state.policy.max_pvs_per_call:
        raise ValueError(
            f"{len(names)} PVs requested; this policy's max_pvs_per_call is "
            f"{state.policy.max_pvs_per_call}"
        )

    denied: list[str] = []
    for name in names:
        decision = state.policy.can_read(name)
        _audit_read_decision(name, decision.allowed, decision.reason)
        if not decision.allowed:
            denied.append(f"{name!r} ({decision.reason})")
    if denied:
        raise PolicyDenied("denied by policy: " + "; ".join(denied))

    return list(state.backend.get_many(names, state.policy.default_timeout_s))


# ---------------------------------------------------------------------------
# epics_pv_info
# ---------------------------------------------------------------------------


@mcp.tool()
def epics_pv_info(name: str) -> dict:
    """Metadata for one PV.

    Units, display/control limits (LOPR/HOPR, DRVL/DRVH), precision, enum
    strings, record type, description (.DESC), and whether this PV is
    writable at all under the server's current policy -- lets the model
    explain a value without guessing at its meaning.
    """
    state = _require_state()
    _check_read_or_raise(name)
    info = dict(state.backend.info(name, state.policy.default_timeout_s))
    info["writable"] = state.policy.is_potentially_writable(name)
    return info


# ---------------------------------------------------------------------------
# epics_pv_watch
# ---------------------------------------------------------------------------


@mcp.tool()
def epics_pv_watch(names: list[str], seconds: float = 10.0, interval: float = 1.0) -> dict:
    """Sample one or more PVs at `interval` seconds, for up to `seconds` total.

    A short, bounded monitor that answers "is it still moving?" without a
    loop of epics_pv_get calls. `seconds` is capped by the policy's
    max_watch_seconds; the resulting number of samples is capped by
    max_watch_samples. Returns {pv: [[timestamp, value], ...]}.
    """
    state = _require_state()
    if len(names) > state.policy.max_pvs_per_call:
        raise ValueError(
            f"{len(names)} PVs requested; this policy's max_pvs_per_call is "
            f"{state.policy.max_pvs_per_call}"
        )
    if interval <= 0:
        raise ValueError("interval must be positive")
    if seconds > state.policy.max_watch_seconds:
        raise ValueError(
            f"seconds={seconds} exceeds this policy's max_watch_seconds="
            f"{state.policy.max_watch_seconds}"
        )
    projected_samples = int(seconds / interval) + 1
    if projected_samples > state.policy.max_watch_samples:
        raise ValueError(
            f"{projected_samples} samples requested (seconds/interval+1); this "
            f"policy's max_watch_samples is {state.policy.max_watch_samples}"
        )

    denied: list[str] = []
    for name in names:
        decision = state.policy.can_read(name)
        _audit_read_decision(name, decision.allowed, decision.reason)
        if not decision.allowed:
            denied.append(f"{name!r} ({decision.reason})")
    if denied:
        raise PolicyDenied("denied by policy: " + "; ".join(denied))

    series = state.backend.monitor(names, seconds, interval)
    return {pv: [[t, v] for t, v in points] for pv, points in series.items()}


# ---------------------------------------------------------------------------
# epics_policy_describe
# ---------------------------------------------------------------------------


@mcp.tool()
def epics_policy_describe() -> dict:
    """The effective policy, in plain data.

    Mode, limits, every allow/deny/write rule with the note its author
    wrote for it, and any load-time warnings. Call this before trying
    PVs you are not sure about -- it is cheaper than a denied call, and it
    is how the policy file's own documentation reaches the model.
    """
    state = _require_state()
    described = state.policy.describe()
    described["backend"] = type(state.backend).__name__
    described["catalog_entries"] = len(state.catalog)
    return described


# ---------------------------------------------------------------------------
# epics_pv_search
# ---------------------------------------------------------------------------


@mcp.tool()
def epics_pv_search(pattern: str) -> list[dict]:
    """Search the static PV catalog by keyword.

    Matches PV name or description, case-insensitively -- Channel Access
    itself has no name-search protocol, so this is the only way to discover
    a PV by topic ("guard slit", "ring current"). Listing a PV here does
    not mean it is accessible: check the `readable` / `writable` flags on
    each result.
    """
    state = _require_state()
    hits = search_catalog(state.catalog, pattern)
    return [
        {
            "pv": entry.pv,
            "description": entry.description,
            "readable": state.policy.can_read(entry.pv).allowed,
            "writable": state.policy.is_potentially_writable(entry.pv),
        }
        for entry in hits
    ]


# ---------------------------------------------------------------------------
# epics_pv_put -- NOT decorated with @mcp.tool(). Registered by configure()
# only when the effective policy allows writes at all (PLAN.md 3).
# ---------------------------------------------------------------------------


def epics_pv_put(name: str, value: Any, confirm_token: str | None = None) -> dict:
    """Write one PV, within the bound this server's policy states for it.

    A rule marked confirm: true never writes on the first call: it returns
    {"status": "confirm_required", "token": ..., "current_value": ...,
    "requested_value": ...} instead. Resend the identical name and value
    with that token to actually write. A rule without confirm: true writes
    immediately.

    Raises for anything the policy will never allow regardless of retries:
    wrong mode, a denied PV, no matching write rule, a value outside that
    rule's bound. Returns a JSON status for everything a retry could
    resolve: confirm_required, confirm_invalid (bad/expired/mismatched
    token -- call again with no token for a fresh one), rate_limited, or
    the ok/error outcome of the actual Channel Access put.
    """
    state = _require_state()

    current_reading = state.backend.get(name, state.policy.default_timeout_s)
    current_value = current_reading["value"] if current_reading["connected"] else None

    decision = state.policy.can_write(name, value, current=current_value)
    _audit(
        "write_check", pv=name, value=value, allowed=decision.allowed, reason=decision.reason
    )
    if not decision.allowed:
        raise PolicyDenied(f"{name!r} = {value!r} is not writable: {decision.reason}")

    rule = decision.rule
    assert rule is not None  # can_write only sets allowed=True alongside a matched rule

    # Confirm-gate before the rate limiter: issuing a challenge touches
    # neither the instrument nor the write budget, only a redeemed token
    # followed by an actual put should ever spend a rate-limit slot.
    if rule.confirm:
        if confirm_token is None:
            challenge = state.confirm_store.issue(name, value)
            return {
                "status": "confirm_required",
                "token": challenge.token,
                "expires_in_s": state.confirm_store.ttl_s,
                "pv": name,
                "current_value": current_value,
                "requested_value": value,
                "rule": f"{rule.pattern}  {rule.describe_constraint()}",
            }
        ok, reason = state.confirm_store.redeem(confirm_token, pv=name, value=value)
        if not ok:
            return {"status": "confirm_invalid", "reason": reason}

    ok, reason = state.limiter.check_and_consume(rule.pattern, rule.rate_limit_per_min)
    if not ok:
        return {"status": "rate_limited", "reason": reason}

    _audit("put", phase="intent", pv=name, old=current_value, new=value, rule=rule.pattern)
    result = state.backend.put(name, value, state.policy.default_timeout_s)
    _audit(
        "put",
        phase="outcome",
        pv=name,
        old=current_value,
        new=value,
        rule=rule.pattern,
        ok=result["connected"],
        error=result["error"],
    )

    if not result["connected"]:
        return {"status": "error", "pv": name, "error": result["error"]}
    return {"status": "ok", "pv": name, "old_value": current_value, "new_value": result["value"]}
