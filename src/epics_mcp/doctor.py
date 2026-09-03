"""Self-check for a policy + environment: `epics-mcp doctor --policy ...`.

Mirrors `aievaluator/doctor.py`'s shape (a list of independent, never-raising
checks) on purpose -- extending a pattern the sibling project already
validated rather than inventing a new one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from epics_mcp.catalog import load_catalog
from epics_mcp.policy import Policy, PolicyError


class CheckResult(TypedDict):
    name: str
    ok: bool
    detail: str


def check_policy(policy_path: Path, *, force_readonly: bool = False) -> CheckResult:
    try:
        policy = Policy.load(policy_path, force_readonly=force_readonly)
    except PolicyError as exc:
        return {"name": "policy", "ok": False, "detail": str(exc)}
    detail = (
        f"mode={policy.effective_mode}, {len(policy.allow)} allow, "
        f"{len(policy.deny)} deny, {len(policy.writes)} write rule(s)"
    )
    if policy.warnings:
        detail += "; warnings: " + " | ".join(policy.warnings)
    return {"name": "policy", "ok": True, "detail": detail}


def check_catalog(policy: Policy) -> CheckResult:
    if policy.catalog_path is None:
        return {"name": "catalog", "ok": False, "detail": "policy sets no catalog:"}
    if not policy.catalog_path.is_file():
        return {"name": "catalog", "ok": False, "detail": f"missing file: {policy.catalog_path}"}
    entries = load_catalog(policy.catalog_path)
    return {
        "name": "catalog",
        "ok": True,
        "detail": f"{len(entries)} entries at {policy.catalog_path}",
    }


def check_audit_writable(policy: Policy) -> CheckResult:
    if policy.audit is None:
        return {"name": "audit", "ok": False, "detail": "policy sets no audit: block"}
    try:
        policy.audit.path.parent.mkdir(parents=True, exist_ok=True)
        probe = policy.audit.path.parent / ".epics-mcp-doctor-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return {"name": "audit", "ok": False, "detail": str(exc)}
    return {"name": "audit", "ok": True, "detail": str(policy.audit.path)}


def check_pyepics() -> CheckResult:
    try:
        import epics  # noqa: F401
    except ImportError as exc:
        return {"name": "pyepics", "ok": False, "detail": str(exc)}
    return {"name": "pyepics", "ok": True, "detail": "pyepics importable"}


def check_representative_pv(policy: Policy, *, pyepics_ok: bool) -> CheckResult:
    if not pyepics_ok:
        return {"name": "ca_connection", "ok": False, "detail": "skipped -- pyepics not importable"}
    representative = next((r.pattern for r in policy.allow if r.is_exact), None)
    if representative is None:
        return {
            "name": "ca_connection",
            "ok": False,
            "detail": "skipped -- no exact-name allow rule in this policy to test against",
        }
    from epics_mcp.ca_client import PyepicsBackend

    backend = PyepicsBackend()
    reading = backend.get(representative, timeout=policy.default_timeout_s)
    backend.close()
    if not reading["connected"]:
        return {
            "name": "ca_connection",
            "ok": False,
            "detail": f"could not connect to {representative}: {reading['error']}",
        }
    return {"name": "ca_connection", "ok": True, "detail": f"connected to {representative}"}


def run_doctor(policy_path: Path, *, force_readonly: bool = False) -> list[CheckResult]:
    """Run every check in order, short-circuiting after a failed policy load
    (nothing else can be checked without a policy)."""
    policy_check = check_policy(policy_path, force_readonly=force_readonly)
    results: list[CheckResult] = [policy_check]
    if not policy_check["ok"]:
        return results

    policy = Policy.load(policy_path, force_readonly=force_readonly)
    results.append(check_catalog(policy))
    results.append(check_audit_writable(policy))
    pyepics_check = check_pyepics()
    results.append(pyepics_check)
    results.append(check_representative_pv(policy, pyepics_ok=pyepics_check["ok"]))
    return results


def format_report(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        status = "OK" if r["ok"] else "FAIL"
        lines.append(f"[{status}] {r['name']}: {r['detail']}")
    return "\n".join(lines)


def all_ok(results: list[CheckResult]) -> bool:
    return all(r["ok"] for r in results)
