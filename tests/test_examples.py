"""Properties of the shipped example policies and catalog (PLAN.md 7).

These exercise the real `Policy` engine, not just YAML shape -- the
YAML-shape checks that used to live here as a stand-in before `policy.py`
existed are now in `tests/test_scaffolding.py` (kept minimal) and, more
usefully, folded into `tests/test_policy.py`'s "shipped examples" section,
which calls `Policy.load()` directly. This file covers the properties that
are specifically about `examples/`, not about the engine in general:
catalog/policy consistency, and the human-facing "every PV is accounted
for" completeness check that has no safety consequence (default deny
already covers it) but matters for reviewability.

Editing an example policy or the catalog without updating this test is
meant to fail CI.
"""

from __future__ import annotations

import pathlib

import pytest

from epics_mcp.policy import Policy

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
CATALOG = EXAMPLES / "pv_catalog_usaxs.txt"
POLICY_FILES = sorted(EXAMPLES.glob("policy_*.yaml"))


def _catalog_pvs() -> list[str]:
    pvs = []
    for line in CATALOG.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pvs.append(stripped.split(maxsplit=1)[0])
    return pvs


def test_catalog_referenced_by_every_policy_exists():
    for path in POLICY_FILES:
        policy = Policy.load(path)
        assert policy.catalog_path is not None, f"{path.name} sets no catalog:"
        assert policy.catalog_path.is_file(), f"{path.name} points at a missing catalog"


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_every_catalog_pv_is_explicitly_allowed_or_denied(path):
    """No catalog entry should silently fall through to default-deny.

    Default deny is safe either way, but a catalog entry nobody's allow or
    deny rule mentions is a sign the catalog and the policy have drifted
    apart -- `epics_pv_search` would return a PV that `epics_pv_get` then
    refuses for a reason a reviewer can't see by reading the policy file.
    """
    policy = Policy.load(path)
    unaccounted = [
        pv
        for pv in _catalog_pvs()
        if not any(r.matches(pv) for r in policy.allow)
        and not any(r.matches(pv) for r in policy.deny)
    ]
    assert not unaccounted, f"{path.name}: catalog PVs matching no rule at all: {unaccounted}"


def test_readonly_example_matches_every_write_capable_pv_the_staff_example_allows():
    """The user policy should read everything the staff policy can read.

    Not a hard requirement in general, but true by construction for these
    two examples (staff = user's reads + a small writable subset), and
    worth pinning down: if this ever needs to become false on purpose,
    that's a deliberate policy decision, not a drift nobody noticed.
    """
    readonly = Policy.load(EXAMPLES / "policy_usaxs_readonly.yaml")
    staff = Policy.load(EXAMPLES / "policy_usaxs_staff.yaml")
    for pv in _catalog_pvs():
        if staff.can_read(pv).allowed:
            assert readonly.can_read(pv).allowed, f"staff can read {pv} but usaxs-user cannot"


def test_no_example_policy_can_write_pss_or_storage_ring_or_undulator():
    for path in POLICY_FILES:
        policy = Policy.load(path)
        for pv in [
            "PA:12ID:STA_A_BEAMREADY_PL.VAL",
            "PA:12ID:STA_C_BEAMREADY_PL.VAL",
            "XFD:srCurrent",
            "S12ID:USID:EnergySetC.VAL",
            "S12ID:USID:HarmonicValueC",
        ]:
            assert not policy.can_write(pv, 1).allowed, f"{path.name} can write {pv}"


def test_staff_example_write_rules_are_all_verified_or_flagged():
    """Every writes: rule is either confirmed safe or carries a TODO(verify) note.

    This is the one property that can't be checked by the engine: whether a
    PV name and its bound were actually checked against the instrument. The
    file's own convention (see its header) is to say so in `note:`.
    """
    policy = Policy.load(EXAMPLES / "policy_usaxs_staff.yaml")
    for rule in policy.writes:
        assert rule.note, f"write rule {rule.pattern!r} has no note explaining its provenance"
