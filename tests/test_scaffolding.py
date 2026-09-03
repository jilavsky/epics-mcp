"""Scaffolding-level checks, written before any implementation exists.

These do two jobs. The dull one is keeping CI meaningful from the first
commit. The useful one is asserting the safety properties of the *shipped
example policies* as data -- those properties (PLAN.md 7) hold whether or
not the policy engine exists yet, and they are the ones a reviewer would
otherwise have to re-check by eye every time someone edits a YAML file.

When `epics_mcp.policy` lands (PLAN.md 6, phase 1), the example assertions
here move into `tests/test_examples.py` and are re-expressed through
`Policy.load()` -- at which point they test the engine as well as the files.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

POLICY_FILES = sorted(EXAMPLES.glob("policy_*.yaml"))

# Stations that belong to other groups. Nothing this package ships may read
# or write them, in any policy, under any mode.
FOREIGN_STATION_PREFIXES = ("12idb", "12idd")


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_package_imports_and_reports_a_version():
    import epics_mcp

    assert epics_mcp.__version__


def test_examples_directory_is_populated():
    assert POLICY_FILES, "no example policies found -- examples/policy_*.yaml"
    assert (EXAMPLES / "pv_catalog_usaxs.txt").is_file()


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_policy_is_valid_yaml_with_the_expected_top_level_keys(path):
    policy = _load(path)
    assert isinstance(policy, dict)
    assert policy["version"] == 1
    assert policy["mode"] in {"read-only", "read-write"}
    for key in ("default_timeout_s", "max_pvs_per_call", "max_watch_seconds"):
        assert key in policy, f"{path.name} is missing {key}"
    # Default deny is only meaningful if the lists are present, even empty.
    assert isinstance(policy.get("allow", []), list)
    assert isinstance(policy.get("deny", []), list)
    assert isinstance(policy.get("writes", []), list)


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_every_rule_carries_a_pattern(path):
    policy = _load(path)
    for section in ("allow", "deny", "writes"):
        for i, rule in enumerate(policy.get(section) or []):
            assert isinstance(rule, dict), f"{path.name}: {section}[{i}] is not a mapping"
            assert rule.get("pattern"), f"{path.name}: {section}[{i}] has no pattern"


def test_readonly_example_grants_no_writes():
    """The user-facing policy must grant nothing writable, whatever `mode` says.

    Asserted independently of `mode` on purpose: flipping the mode line alone
    must not turn this file into a write-capable policy.
    """
    policy = _load(EXAMPLES / "policy_usaxs_readonly.yaml")
    assert policy["mode"] == "read-only"
    assert policy.get("writes") == []


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_no_policy_pattern_reaches_a_foreign_station(path):
    """No allow or write rule may name another group's station (PLAN.md 4.2)."""
    policy = _load(path)
    for section in ("allow", "writes"):
        for rule in policy.get(section) or []:
            pattern = rule["pattern"].removeprefix("re:").lstrip("^")
            lowered = pattern.lower()
            for prefix in FOREIGN_STATION_PREFIXES:
                assert not lowered.startswith(prefix), (
                    f"{path.name}: {section} rule {rule['pattern']!r} names "
                    f"foreign station {prefix}"
                )


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_every_write_rule_is_bounded(path):
    """A write rule with no `range` and no `enum` is an unbounded write."""
    for rule in _load(path).get("writes") or []:
        assert ("range" in rule) or ("enum" in rule), (
            f"{path.name}: write rule {rule['pattern']!r} has neither range nor enum"
        )


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_write_ranges_are_ordered_numeric_pairs(path):
    for rule in _load(path).get("writes") or []:
        if "range" not in rule:
            continue
        lo, hi = rule["range"]
        assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))
        assert lo < hi, f"{path.name}: {rule['pattern']!r} has range [{lo}, {hi}]"


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda p: p.name)
def test_no_write_rule_targets_a_builtin_denied_field(path):
    """Fields that rewire a record are never writable (PLAN.md 4.4).

    The loader will enforce this too; catching it in the shipped examples
    means a reviewer never has to wonder whether a rule is dead or dangerous.
    """
    forbidden = (
        ".PROC", ".STOP", ".SCAN", ".FLNK", ".INP", ".OUT", ".CALC",
        ".SDIS", ".DISA", ".DISV", ".SIML", ".SIOL", ".TPRO", ".UDF",
    )
    for rule in _load(path).get("writes") or []:
        pattern = rule["pattern"].upper()
        for field in forbidden:
            assert not pattern.endswith(field), (
                f"{path.name}: write rule {rule['pattern']!r} targets {field}"
            )


def test_write_enabled_example_declares_a_global_write_ceiling():
    """A read-write policy without `max_writes_per_min` has no runaway ceiling."""
    for path in POLICY_FILES:
        policy = _load(path)
        if policy["mode"] == "read-write":
            assert policy.get("max_writes_per_min"), (
                f"{path.name} is read-write but sets no max_writes_per_min"
            )


def test_pv_catalog_parses_and_every_entry_has_a_description():
    catalog = (EXAMPLES / "pv_catalog_usaxs.txt").read_text().splitlines()
    entries = 0
    for lineno, line in enumerate(catalog, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(maxsplit=1)
        assert len(parts) == 2, f"pv_catalog_usaxs.txt:{lineno} has no description"
        pv, description = parts
        assert ":" in pv, f"pv_catalog_usaxs.txt:{lineno}: {pv!r} is not a PV name"
        assert description.strip()
        entries += 1
    assert entries > 20, "catalog looks truncated"
