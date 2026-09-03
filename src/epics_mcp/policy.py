"""Load and evaluate an epics-mcp policy file.

This module is the safety boundary described in ``PLAN.md`` section 4. It
imports nothing beyond the standard library and ``yaml`` on purpose (see
``PLAN.md`` section 5) so it stays auditable on its own, and so a future
consumer (``aievaluator``'s own ``epics_io``, per the design document) can
depend on it without dragging in ``mcp`` or ``pyepics``.

Nothing here talks to EPICS. ``Policy.can_read`` / ``Policy.can_write``
answer "is this allowed", never "what is the value" -- that is
``ca_client.py``'s job, gated by the ``Decision`` this module returns.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Fields that change what a record *is* or how it is wired. Never writable,
# whatever a policy's ``writes:`` patterns say -- see PLAN.md 4.4.
BUILTIN_WRITE_DENIED_FIELDS = (
    ".PROC", ".STOP", ".SCAN", ".FLNK", ".INP", ".OUT", ".CALC",
    ".SDIS", ".DISA", ".DISV", ".SIML", ".SIOL", ".TPRO", ".UDF",
)

_GLOB_WILDCARD_CHARS = "*?["
_SUPPORTED_VERSION = 1


class PolicyError(Exception):
    """The policy file is missing, malformed, or internally contradictory."""


def _is_regex_pattern(pattern: str) -> bool:
    return pattern.startswith("re:")


def _is_exact_name(pattern: str) -> bool:
    """True for a pattern that names exactly one PV -- no glob, no regex.

    This is the one case PLAN.md 4.3 exempts from the deny-shadowing checks:
    an exact name is auditable by eye, so it is allowed to punch a single,
    named hole in a broad ``deny:`` for reads.
    """
    return not _is_regex_pattern(pattern) and not any(c in pattern for c in _GLOB_WILDCARD_CHARS)


def _compile_matcher(pattern: str) -> Callable[[str], bool]:
    if _is_regex_pattern(pattern):
        expr = pattern[len("re:"):]
        try:
            compiled = re.compile(expr)
        except re.error as exc:
            raise PolicyError(f"invalid regex pattern {pattern!r}: {exc}") from exc
        return lambda name, _c=compiled: _c.fullmatch(name) is not None
    return lambda name, _p=pattern: fnmatch.fnmatchcase(name, _p)


# --- the deny-shadowing heuristic (PLAN.md 4.3) -----------------------------
#
# Detecting whether two arbitrary glob/regex patterns can both match some
# common string is, in general, hard. We deliberately do not attempt it in
# general. Instead we recognise one narrow, common shape -- "a literal
# prefix followed by a single trailing wildcard" (``usx*``, ``re:^12idb.*``)
# -- because that is the shape every station/IOC-prefix rule in practice
# takes, and it is the shape the illustrative conflict in PLAN.md 4.3 uses
# (``PA:*`` vs ``PA:12ID:*``). Patterns of any other shape (``*.PROC``,
# middle-wildcard patterns like ``PA:12ID:STA_*_BEAMREADY_PL.VAL``) are
# skipped by this check entirely -- not because they cannot conflict, but
# because a prefix-only heuristic would either miss them or produce false
# positives (an empty literal prefix for a leading ``*`` would otherwise
# "overlap" with everything). This is a load-time footgun check, not a
# formal verifier: it catches the case in the example, not every case.
#
# The check is *directional*, not symmetric. "Broad allow, narrower deny
# carve-out" -- ``allow: usx*`` plus ``deny: usxSECRET:*``, or the staff
# example's ``allow: 12ida2:*`` plus a hypothetical narrower deny inside it
# -- is a completely ordinary policy: deny wins inside its narrower slice,
# allow wins everywhere else in its broader one. Only the opposite shape is
# a contradiction worth failing to load: an allow rule whose entire prefix
# already sits inside a deny rule's prefix does *nothing*, ever, because
# deny always wins wherever the allow could possibly match.

_GLOB_PREFIX_RE = re.compile(r"^([^*?\[]+)\*$")
_REGEX_PREFIX_RE = re.compile(r"^\^?([^.^$*+?{}\[\]\\|()]+)\.\*\$?$")


def _prefix_glob_literal(pattern: str) -> str | None:
    """The literal prefix of a "prefix-glob" pattern, or None if it isn't one."""
    if _is_regex_pattern(pattern):
        m = _REGEX_PREFIX_RE.match(pattern[len("re:"):])
    else:
        m = _GLOB_PREFIX_RE.match(pattern)
    return m.group(1) if m else None


def _deny_prefix_shadows_allow_prefix(*, deny_pattern: str, allow_pattern: str) -> bool:
    """True if `allow_pattern`'s whole prefix-glob range sits inside `deny_pattern`'s."""
    deny_prefix = _prefix_glob_literal(deny_pattern)
    allow_prefix = _prefix_glob_literal(allow_pattern)
    if deny_prefix is None or allow_prefix is None:
        return False
    return allow_prefix.startswith(deny_prefix)


@dataclass(frozen=True)
class Rule:
    pattern: str
    note: str
    is_exact: bool
    matcher: Callable[[str], bool] = field(repr=False, compare=False)

    def matches(self, name: str) -> bool:
        return self.matcher(name)


@dataclass(frozen=True)
class WriteRule(Rule):
    range: tuple[float, float] | None = None
    enum: tuple[Any, ...] | None = None
    step: float | None = None
    max_delta: float | None = None
    units: str | None = None
    confirm: bool = False
    rate_limit_per_min: int | None = None

    def describe_constraint(self) -> str:
        parts = []
        if self.range is not None:
            parts.append(f"range {list(self.range)}")
        if self.enum is not None:
            parts.append(f"enum {list(self.enum)}")
        if self.step is not None:
            parts.append(f"step {self.step}")
        if self.max_delta is not None:
            parts.append(f"max_delta {self.max_delta}")
        if self.units:
            parts.append(f"units {self.units}")
        if self.confirm:
            parts.append("confirm required")
        if self.rate_limit_per_min is not None:
            parts.append(f"rate_limit {self.rate_limit_per_min}/min")
        return "  ".join(parts)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    rule: WriteRule | None = None


@dataclass(frozen=True)
class AuditConfig:
    path: Path
    log_reads: bool = False
    log_denials: bool = True
    max_bytes: int | None = None
    keep: int | None = None


def _make_rule(raw: dict[str, Any], *, section: str) -> Rule:
    pattern = raw.get("pattern")
    if not pattern or not isinstance(pattern, str):
        raise PolicyError(f"{section} rule is missing a string 'pattern': {raw!r}")
    return Rule(
        pattern=pattern,
        note=str(raw.get("note", "")),
        is_exact=_is_exact_name(pattern),
        matcher=_compile_matcher(pattern),
    )


def _make_write_rule(raw: dict[str, Any]) -> WriteRule:
    pattern = raw.get("pattern")
    if not pattern or not isinstance(pattern, str):
        raise PolicyError(f"writes rule is missing a string 'pattern': {raw!r}")

    range_ = raw.get("range")
    if range_ is not None:
        if len(range_) != 2:
            raise PolicyError(f"writes rule {pattern!r}: range must be [lo, hi]")
        lo, hi = range_
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            raise PolicyError(f"writes rule {pattern!r}: range values must be numeric")
        if math.isnan(lo) or math.isnan(hi) or math.isinf(lo) or math.isinf(hi):
            raise PolicyError(f"writes rule {pattern!r}: range must be finite")
        if lo >= hi:
            raise PolicyError(f"writes rule {pattern!r}: range [{lo}, {hi}] is not lo < hi")
        range_ = (float(lo), float(hi))

    enum = raw.get("enum")
    if enum is not None:
        enum = tuple(enum)
        if not enum:
            raise PolicyError(f"writes rule {pattern!r}: enum must not be empty")

    if range_ is None and enum is None:
        raise PolicyError(
            f"writes rule {pattern!r} has neither 'range' nor 'enum' -- "
            "an unbounded write is not a policy, see PLAN.md 4.5"
        )

    rate_limit = raw.get("rate_limit_per_min")
    if rate_limit is not None and (not isinstance(rate_limit, int) or rate_limit <= 0):
        raise PolicyError(f"writes rule {pattern!r}: rate_limit_per_min must be a positive int")

    for field_name in BUILTIN_WRITE_DENIED_FIELDS:
        if pattern.upper().endswith(field_name):
            raise PolicyError(
                f"writes rule {pattern!r} targets {field_name}, which is never "
                "writable (PLAN.md 4.4) -- remove this rule"
            )

    return WriteRule(
        pattern=pattern,
        note=str(raw.get("note", "")),
        is_exact=_is_exact_name(pattern),
        matcher=_compile_matcher(pattern),
        range=range_,
        enum=enum,
        step=raw.get("step"),
        max_delta=raw.get("max_delta"),
        units=raw.get("units"),
        confirm=bool(raw.get("confirm", False)),
        rate_limit_per_min=rate_limit,
    )


def _expand_path(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw)))


class Policy:
    """The parsed, validated, immutable content of one policy file."""

    def __init__(
        self,
        *,
        source_path: Path,
        sha256: str,
        mode: str,
        force_readonly: bool,
        default_timeout_s: float,
        max_pvs_per_call: int,
        max_watch_seconds: int,
        max_watch_samples: int,
        max_writes_per_min: int | None,
        catalog_path: Path | None,
        audit: AuditConfig | None,
        allow: tuple[Rule, ...],
        deny: tuple[Rule, ...],
        writes: tuple[WriteRule, ...],
        builtin_write_denies_disabled: bool,
        warnings: tuple[str, ...],
    ) -> None:
        self.source_path = source_path
        self.sha256 = sha256
        self.mode = mode
        self.force_readonly = force_readonly
        self.default_timeout_s = default_timeout_s
        self.max_pvs_per_call = max_pvs_per_call
        self.max_watch_seconds = max_watch_seconds
        self.max_watch_samples = max_watch_samples
        self.max_writes_per_min = max_writes_per_min
        self.catalog_path = catalog_path
        self.audit = audit
        self.allow = allow
        self.deny = deny
        self.writes = writes
        self.builtin_write_denies_disabled = builtin_write_denies_disabled
        self.warnings = warnings

    @property
    def effective_mode(self) -> str:
        """``mode`` after the ``--readonly`` command-line override.

        The override can only narrow, never widen: PLAN.md 4.2 -- "--readonly
        on the command line beats the file, never the reverse."
        """
        return "read-only" if self.force_readonly else self.mode

    @property
    def writes_enabled(self) -> bool:
        return self.effective_mode == "read-write"

    # -- loading --------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path, *, force_readonly: bool = False) -> Policy:
        path = Path(path)
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise PolicyError(f"cannot read policy file {path}: {exc}") from exc

        try:
            data = yaml.safe_load(raw_bytes)
        except yaml.YAMLError as exc:
            raise PolicyError(f"{path} is not valid YAML: {exc}") from exc

        if not isinstance(data, dict):
            raise PolicyError(f"{path}: top level must be a mapping")

        version = data.get("version")
        if version != _SUPPORTED_VERSION:
            raise PolicyError(
                f"{path}: unsupported policy version {version!r} "
                f"(this build understands version {_SUPPORTED_VERSION})"
            )

        mode = data.get("mode")
        if mode not in ("read-only", "read-write"):
            raise PolicyError(f"{path}: mode must be 'read-only' or 'read-write', got {mode!r}")

        allow = tuple(_make_rule(r, section="allow") for r in (data.get("allow") or []))
        deny = tuple(_make_rule(r, section="deny") for r in (data.get("deny") or []))
        writes = tuple(_make_write_rule(r) for r in (data.get("writes") or []))

        warnings: list[str] = []

        # deny-shadowing check (PLAN.md 4.3): a non-exact allow rule that
        # overlaps a deny rule's prefix is a load error, not a silent
        # widening. Exact-name allows are exempt by design.
        for allow_rule in allow:
            if allow_rule.is_exact:
                continue
            for deny_rule in deny:
                if _deny_prefix_shadows_allow_prefix(
                    deny_pattern=deny_rule.pattern, allow_pattern=allow_rule.pattern
                ):
                    raise PolicyError(
                        f"{path}: allow pattern {allow_rule.pattern!r} overlaps "
                        f"deny pattern {deny_rule.pattern!r}. Deny always wins, "
                        "so this allow rule is silently doing nothing where it "
                        "matters -- narrow the allow to an exact PV name if you "
                        "mean to punch a hole in the deny (PLAN.md 4.3), or "
                        "narrow the deny if you mean to open this prefix."
                    )

        if mode == "read-write" and writes and data.get("max_writes_per_min") is None:
            warnings.append(
                "mode is read-write with writes: rules but no policy-wide "
                "max_writes_per_min -- a runaway agent has no global ceiling"
            )

        max_writes_per_min = data.get("max_writes_per_min")
        if max_writes_per_min is not None and (
            not isinstance(max_writes_per_min, int) or max_writes_per_min <= 0
        ):
            raise PolicyError(f"{path}: max_writes_per_min must be a positive int")

        catalog_raw = data.get("catalog")
        catalog_path = None
        if catalog_raw:
            catalog_path = (path.parent / catalog_raw).resolve()

        audit_raw = data.get("audit")
        audit_cfg = None
        if audit_raw:
            if not audit_raw.get("path"):
                raise PolicyError(f"{path}: audit block present but has no 'path'")
            audit_cfg = AuditConfig(
                path=_expand_path(audit_raw["path"]),
                log_reads=bool(audit_raw.get("log_reads", False)),
                log_denials=bool(audit_raw.get("log_denials", True)),
                max_bytes=audit_raw.get("max_bytes"),
                keep=audit_raw.get("keep"),
            )
        else:
            warnings.append("no audit: block -- gated operations will not be logged anywhere")

        builtin_disabled = bool(data.get("disable_builtin_write_denies", False))
        if builtin_disabled:
            warnings.append(
                "disable_builtin_write_denies is set -- .PROC/.STOP/.SCAN/... "
                "and friends are writable if a writes: rule matches them"
            )

        return cls(
            source_path=path,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            mode=mode,
            force_readonly=force_readonly,
            default_timeout_s=float(data.get("default_timeout_s", 3.0)),
            max_pvs_per_call=int(data.get("max_pvs_per_call", 50)),
            max_watch_seconds=int(data.get("max_watch_seconds", 30)),
            max_watch_samples=int(data.get("max_watch_samples", 300)),
            max_writes_per_min=max_writes_per_min,
            catalog_path=catalog_path,
            audit=audit_cfg,
            allow=allow,
            deny=deny,
            writes=writes,
            builtin_write_denies_disabled=builtin_disabled,
            warnings=tuple(warnings),
        )

    # -- evaluation -------------------------------------------------------

    def can_read(self, pv: str) -> Decision:
        deny_match = next((r for r in self.deny if r.matches(pv)), None)
        allow_match = next((r for r in self.allow if r.matches(pv)), None)

        if deny_match is not None:
            # The one exception (PLAN.md 4.3): an exact-name allow overrides
            # a shadowing deny, for reads only.
            if allow_match is not None and allow_match.is_exact:
                return Decision(
                    True,
                    f"allow (exact-name exception): {allow_match.pattern!r} "
                    f"overrides deny: {deny_match.pattern!r}",
                )
            return Decision(False, f"deny: {deny_match.pattern!r}")

        if allow_match is not None:
            return Decision(True, f"allow: {allow_match.pattern!r}")

        return Decision(False, "default deny (no allow rule matches)")

    def can_write(self, pv: str, value: Any, *, current: float | None = None) -> Decision:
        if not self.writes_enabled:
            reason = (
                "read-only override (--readonly)" if self.force_readonly
                else "mode: read-only"
            )
            return Decision(False, reason)

        if not self.builtin_write_denies_disabled:
            upper = pv.upper()
            for builtin_field in BUILTIN_WRITE_DENIED_FIELDS:
                if upper.endswith(builtin_field):
                    return Decision(False, f"builtin: {builtin_field} is never writable")

        # Deny always wins for writes -- no exact-name exception here. This
        # is what lets policy_usaxs_staff.yaml guarantee a PSS record can
        # never be reached by any future writes: edit (PLAN.md 4.3).
        deny_match = next((r for r in self.deny if r.matches(pv)), None)
        if deny_match is not None:
            return Decision(False, f"deny: {deny_match.pattern!r}")

        write_rule = next((r for r in self.writes if r.matches(pv)), None)
        if write_rule is None:
            return Decision(False, "no write rule matches")

        violation = self._check_constraint(write_rule, value, current=current)
        if violation is not None:
            return Decision(False, violation, rule=write_rule)

        return Decision(True, f"writes: {write_rule.pattern!r}", rule=write_rule)

    @staticmethod
    def _check_constraint(rule: WriteRule, value: Any, *, current: float | None) -> str | None:
        if rule.enum is not None:
            if value not in rule.enum:
                return f"value {value!r} is not one of {list(rule.enum)}"
            return None

        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"value {value!r} is not numeric"
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return "value is NaN or infinite"

        if rule.range is not None:
            lo, hi = rule.range
            if not (lo <= value <= hi):
                return f"value {value} is outside range [{lo}, {hi}]"

        if rule.step is not None and rule.step > 0:
            # Guards a fat-finger 10x-style error: the requested value must
            # land on the rule's step grid, measured from the low end of the
            # range if one is set, otherwise from zero.
            origin = rule.range[0] if rule.range is not None else 0.0
            offset = value - origin
            remainder = offset % rule.step
            if remainder > 1e-9 and (rule.step - remainder) > 1e-9:
                return f"value {value} is not a multiple of step {rule.step} from {origin}"

        if rule.max_delta is not None:
            if current is None:
                # Fails closed: PLAN.md 4.5 -- "requires a successful read
                # first, and fails closed if the read fails."
                return "max_delta rule requires the current value, which could not be read"
            if abs(value - current) > rule.max_delta:
                return f"value {value} differs from current {current} by more than {rule.max_delta}"

        return None

    # -- introspection ------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "source": str(self.source_path),
            "sha256": self.sha256,
            "mode": self.mode,
            "effective_mode": self.effective_mode,
            "force_readonly": self.force_readonly,
            "default_timeout_s": self.default_timeout_s,
            "max_pvs_per_call": self.max_pvs_per_call,
            "max_watch_seconds": self.max_watch_seconds,
            "max_watch_samples": self.max_watch_samples,
            "max_writes_per_min": self.max_writes_per_min,
            "builtin_write_denies_disabled": self.builtin_write_denies_disabled,
            "allow": [{"pattern": r.pattern, "note": r.note} for r in self.allow],
            "deny": [{"pattern": r.pattern, "note": r.note} for r in self.deny],
            "writes": [
                {"pattern": r.pattern, "note": r.note, "constraint": r.describe_constraint()}
                for r in self.writes
            ],
            "warnings": list(self.warnings),
        }
