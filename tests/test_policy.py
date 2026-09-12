"""Tests for the policy engine -- the safety boundary (PLAN.md 4).

Every rule in PLAN.md 4.2/4.3/4.4/4.5 gets rows here, including the ones
that must *fail to load*.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from epics_mcp.policy import Policy, PolicyError

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


def write_policy(tmp_path: pathlib.Path, data: dict, name: str = "policy.yaml") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def base_policy(**overrides) -> dict:
    data = {
        "version": 1,
        "mode": "read-only",
        "default_timeout_s": 3.0,
        "max_pvs_per_call": 50,
        "max_watch_seconds": 30,
        "allow": [],
        "deny": [],
        "writes": [],
    }
    data.update(overrides)
    return data


# --- loading ----------------------------------------------------------


def test_missing_file_raises(tmp_path):
    with pytest.raises(PolicyError):
        Policy.load(tmp_path / "does_not_exist.yaml")


def test_not_yaml_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("{not: valid: yaml: at: all")
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_top_level_must_be_a_mapping(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n")
    with pytest.raises(PolicyError):
        Policy.load(path)


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_unsupported_version_raises(tmp_path, version):
    path = write_policy(tmp_path, base_policy(version=version))
    with pytest.raises(PolicyError):
        Policy.load(path)


@pytest.mark.parametrize("mode", [None, "readonly", "READ-ONLY", ""])
def test_invalid_mode_raises(tmp_path, mode):
    path = write_policy(tmp_path, base_policy(mode=mode))
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_empty_allow_is_valid_and_grants_nothing(tmp_path):
    path = write_policy(tmp_path, base_policy())
    policy = Policy.load(path)
    assert policy.can_read("anything:at:all").allowed is False


def test_malformed_regex_is_a_load_error(tmp_path):
    path = write_policy(tmp_path, base_policy(allow=[{"pattern": "re:(unclosed"}]))
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_rule_without_pattern_raises(tmp_path):
    path = write_policy(tmp_path, base_policy(allow=[{"note": "oops, no pattern"}]))
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_write_rule_without_range_or_enum_raises(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo"}]),
    )
    with pytest.raises(PolicyError, match="neither"):
        Policy.load(path)


@pytest.mark.parametrize("range_", [[1], [1, 2, 3], ["a", "b"], [5, 1]])
def test_write_rule_bad_range_raises(tmp_path, range_):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": range_}]),
    )
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_write_rule_infinite_range_raises(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [float("-inf"), 10.0]}],
        ),
    )
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_write_rule_empty_enum_raises(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "enum": []}]),
    )
    with pytest.raises(PolicyError):
        Policy.load(path)


@pytest.mark.parametrize(
    "field_name", [".PROC", ".STOP", ".SCAN", ".FLNK", ".proc", ".Stop"]
)
def test_write_rule_targeting_builtin_denied_field_raises(tmp_path, field_name):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": f"usx:foo{field_name}", "range": [0, 1]}],
        ),
    )
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_audit_block_without_path_raises(tmp_path):
    path = write_policy(tmp_path, base_policy(audit={"log_reads": True}))
    with pytest.raises(PolicyError):
        Policy.load(path)


# --- the deny-shadowing conflict (PLAN.md 4.3) --------------------------


def test_wildcard_allow_overlapping_deny_prefix_raises(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            deny=[{"pattern": "PA:*"}],
            allow=[{"pattern": "PA:12ID:*"}],
        ),
    )
    with pytest.raises(PolicyError, match="overlaps"):
        Policy.load(path)


def test_wildcard_allow_overlapping_regex_deny_prefix_raises(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            deny=[{"pattern": "re:^12idb.*"}],
            allow=[{"pattern": "12idb*"}],
        ),
    )
    with pytest.raises(PolicyError, match="overlaps"):
        Policy.load(path)


def test_exact_name_allow_overlapping_deny_prefix_loads_fine(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            deny=[{"pattern": "PA:*"}],
            allow=[{"pattern": "PA:12ID:STA_A_BEAMREADY_PL.VAL"}],
        ),
    )
    policy = Policy.load(path)  # must not raise
    assert policy.can_read("PA:12ID:STA_A_BEAMREADY_PL.VAL").allowed is True


def test_non_overlapping_prefixes_load_fine(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            deny=[{"pattern": "12idb*"}],
            allow=[{"pattern": "12idc:*"}, {"pattern": "usx*"}],
        ),
    )
    Policy.load(path)  # must not raise


def test_suffix_glob_deny_does_not_falsely_conflict_with_prefix_allow(tmp_path):
    """`*.PROC` has an empty literal prefix and must not "overlap" every allow."""
    path = write_policy(
        tmp_path,
        base_policy(
            deny=[{"pattern": "*.PROC"}],
            allow=[{"pattern": "usx*"}],
        ),
    )
    policy = Policy.load(path)  # must not raise
    assert policy.can_read("usx:foo.PROC").allowed is False
    assert policy.can_read("usx:foo.RBV").allowed is True


# --- can_read: default deny, deny-wins, the exact-name exception --------


def test_default_deny_with_no_rules(tmp_path):
    path = write_policy(tmp_path, base_policy())
    policy = Policy.load(path)
    decision = policy.can_read("usx:foo")
    assert decision.allowed is False
    assert "default deny" in decision.reason


def test_allow_grants_read(tmp_path):
    path = write_policy(tmp_path, base_policy(allow=[{"pattern": "usx*"}]))
    policy = Policy.load(path)
    assert policy.can_read("usx:foo").allowed is True
    assert policy.can_read("other:bar").allowed is False


def test_deny_beats_allow_for_reads_when_allow_is_wildcard(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(allow=[{"pattern": "usx*"}], deny=[{"pattern": "usxSECRET:*"}]),
    )
    policy = Policy.load(path)
    assert policy.can_read("usx:foo").allowed is True
    assert policy.can_read("usxSECRET:foo").allowed is False


def test_exact_name_allow_overrides_deny_for_reads(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            deny=[{"pattern": "PA:*"}],
            allow=[{"pattern": "PA:12ID:STA_A_BEAMREADY_PL.VAL"}],
        ),
    )
    policy = Policy.load(path)
    decision = policy.can_read("PA:12ID:STA_A_BEAMREADY_PL.VAL")
    assert decision.allowed is True
    assert "exception" in decision.reason
    # A sibling PV under the same deny, with no exact allow, is still denied.
    assert policy.can_read("PA:12ID:STA_C_BEAMREADY_PL.VAL").allowed is False


def test_regex_deny_pattern(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(allow=[{"pattern": "12id*"}], deny=[{"pattern": "re:^12idb.*"}]),
    )
    policy = Policy.load(path)
    assert policy.can_read("12ida2:foo").allowed is True
    assert policy.can_read("12idb1:foo").allowed is False


# --- can_write: mode, override, deny-always-wins, rule matching ---------


def test_write_denied_in_read_only_mode(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-only", writes=[]),
    )
    policy = Policy.load(path)
    decision = policy.can_write("usx:foo", 1.0)
    assert decision.allowed is False
    assert "read-only" in decision.reason


def test_readonly_override_beats_readwrite_file(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            allow=[{"pattern": "usx*"}],
            writes=[{"pattern": "usx:foo", "range": [0, 10]}],
        ),
    )
    policy = Policy.load(path, force_readonly=True)
    decision = policy.can_write("usx:foo", 5.0)
    assert decision.allowed is False
    assert "override" in decision.reason


def test_write_allowed_within_range(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            allow=[{"pattern": "usx*"}],
            writes=[{"pattern": "usx:foo", "range": [0, 10]}],
        ),
    )
    policy = Policy.load(path)
    decision = policy.can_write("usx:foo", 5.0)
    assert decision.allowed is True
    assert decision.rule is not None
    assert decision.rule.pattern == "usx:foo"


def test_write_outside_range_denied(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 10]}],
        ),
    )
    policy = Policy.load(path)
    decision = policy.can_write("usx:foo", 50.0)
    assert decision.allowed is False
    assert "outside range" in decision.reason


def test_write_non_numeric_value_denied_for_range_rule(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
    )
    policy = Policy.load(path)
    assert policy.can_write("usx:foo", "not a number").allowed is False
    assert policy.can_write("usx:foo", float("nan")).allowed is False
    assert policy.can_write("usx:foo", float("inf")).allowed is False


def test_write_enum_rule(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:mode", "enum": ["auto", "manual"]}],
        ),
    )
    policy = Policy.load(path)
    assert policy.can_write("usx:mode", "auto").allowed is True
    assert policy.can_write("usx:mode", "turbo").allowed is False


def test_write_step_rule(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 100], "step": 10}],
        ),
    )
    policy = Policy.load(path)
    assert policy.can_write("usx:foo", 20.0).allowed is True
    assert policy.can_write("usx:foo", 25.0).allowed is False


def test_write_max_delta_requires_current_value(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 1000], "max_delta": 5}],
        ),
    )
    policy = Policy.load(path)
    # No current value supplied -> fails closed (PLAN.md 4.5).
    assert policy.can_write("usx:foo", 10.0).allowed is False
    assert policy.can_write("usx:foo", 10.0, current=8.0).allowed is True
    assert policy.can_write("usx:foo", 10.0, current=1.0).allowed is False


def test_write_no_matching_rule_denied(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
    )
    policy = Policy.load(path)
    decision = policy.can_write("usx:bar", 5.0)
    assert decision.allowed is False
    assert "no write rule" in decision.reason


def test_deny_always_wins_for_writes_even_with_exact_name_allow(tmp_path):
    """PLAN.md 4.3: the exact-name exception applies to reads only.

    A PSS record allowed (exactly) for reading through a broad deny must
    stay unwritable even if a writes: rule later names it exactly.
    """
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            deny=[{"pattern": "PA:*"}],
            allow=[{"pattern": "PA:12ID:STA_A_BEAMREADY_PL.VAL"}],
            writes=[{"pattern": "PA:12ID:STA_A_BEAMREADY_PL.VAL", "enum": [0, 1]}],
        ),
    )
    policy = Policy.load(path)
    assert policy.can_read("PA:12ID:STA_A_BEAMREADY_PL.VAL").allowed is True
    decision = policy.can_write("PA:12ID:STA_A_BEAMREADY_PL.VAL", 1)
    assert decision.allowed is False
    assert "deny" in decision.reason


@pytest.mark.parametrize(
    "pv", ["usx:foo.PROC", "usx:foo.STOP", "usx:foo.SCAN", "usx:foo.proc"]
)
def test_builtin_write_deny_applies_at_runtime_even_if_rule_construction_were_bypassed(
    tmp_path, monkeypatch, pv
):
    """Belt and suspenders: can_write re-checks builtin denies at match time.

    _make_write_rule already refuses to construct a rule targeting these
    fields (tested above); this asserts the runtime check independently, in
    case a rule's *pattern* is broad enough to match a denied field without
    literally ending in it (e.g. matched via a wildcard that happens to
    reach a `.PROC` PV rather than naming it).
    """
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:*", "range": [0, 1]}]),
    )
    policy = Policy.load(path)
    assert policy.can_write(pv, 1).allowed is False


def test_disable_builtin_write_denies_escape_hatch(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(
            mode="read-write",
            disable_builtin_write_denies=True,
            writes=[{"pattern": "usx:*", "range": [0, 1]}],
        ),
    )
    policy = Policy.load(path)
    assert policy.builtin_write_denies_disabled is True
    assert any("disable_builtin_write_denies" in w for w in policy.warnings)
    assert policy.can_write("usx:foo.PROC", 1).allowed is True


# --- sha256, describe, catalog path resolution --------------------------


def test_sha256_changes_when_file_changes(tmp_path):
    path = write_policy(tmp_path, base_policy())
    policy_a = Policy.load(path)
    path.write_text(path.read_text() + "\n# a comment\n")
    policy_b = Policy.load(path)
    assert policy_a.sha256 != policy_b.sha256


def test_describe_returns_plain_dict(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(allow=[{"pattern": "usx*", "note": "instrument"}]),
    )
    policy = Policy.load(path)
    described = policy.describe()
    assert described["mode"] == "read-only"
    assert described["allow"] == [{"pattern": "usx*", "note": "instrument"}]
    assert described["sha256"] == policy.sha256


def test_catalog_path_resolved_relative_to_policy_file(tmp_path):
    (tmp_path / "cat.txt").write_text("usx:foo  a motor\n")
    path = write_policy(tmp_path, base_policy(catalog="cat.txt"))
    policy = Policy.load(path)
    assert policy.catalog_path == (tmp_path / "cat.txt").resolve()


def test_missing_audit_block_warns(tmp_path):
    path = write_policy(tmp_path, base_policy())
    policy = Policy.load(path)
    assert any("audit" in w for w in policy.warnings)


def test_readwrite_without_ceiling_warns(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 1]}]),
    )
    policy = Policy.load(path)
    assert any("max_writes_per_min" in w for w in policy.warnings)


# --- the shipped example policies, exercised through the real engine ---


@pytest.mark.parametrize(
    "example_file", ["policy_usaxs_readonly.yaml", "policy_usaxs_staff.yaml"]
)
def test_shipped_examples_load_without_error(example_file):
    Policy.load(EXAMPLES / example_file)


def test_readonly_example_grants_no_writes_under_any_mode():
    policy = Policy.load(EXAMPLES / "policy_usaxs_readonly.yaml")
    assert policy.writes == ()
    # Even if somehow invoked as read-write, there is nothing to match.
    assert policy.can_write("usxLAX:m58:c0:m1.VAL", 1.0).allowed is False


def test_readonly_example_reads_a_representative_usaxs_pv():
    policy = Policy.load(EXAMPLES / "policy_usaxs_readonly.yaml")
    assert policy.can_read("usxLAX:m58:c0:m1.RBV").allowed is True
    assert policy.can_read("XFD:srCurrent").allowed is True


@pytest.mark.parametrize("pv", ["12idb1:m1.VAL", "12idd3:foo", "12idbANYTHING"])
def test_no_example_policy_reaches_a_foreign_station(pv):
    for example_file in ["policy_usaxs_readonly.yaml", "policy_usaxs_staff.yaml"]:
        policy = Policy.load(EXAMPLES / example_file)
        assert policy.can_read(pv).allowed is False, f"{example_file} read {pv}"
        assert policy.can_write(pv, 0).allowed is False, f"{example_file} wrote {pv}"


def test_staff_example_cannot_write_pss_or_ring_or_undulator():
    policy = Policy.load(EXAMPLES / "policy_usaxs_staff.yaml")
    for pv in [
        "PA:12ID:STA_A_BEAMREADY_PL.VAL",
        "XFD:srCurrent",
        "S12ID:USID:EnergySetC.VAL",
    ]:
        assert policy.can_read(pv).allowed is True, f"expected {pv} readable"
        assert policy.can_write(pv, 1).allowed is False, f"expected {pv} unwritable"


def test_staff_example_can_write_the_verified_scratch_record():
    policy = Policy.load(EXAMPLES / "policy_usaxs_staff.yaml")
    decision = policy.can_write("usxLAX:userCalc3.A", 42.0)
    assert decision.allowed is True


def test_staff_example_readonly_flag_disables_all_writes():
    policy = Policy.load(EXAMPLES / "policy_usaxs_staff.yaml", force_readonly=True)
    assert policy.can_write("usxLAX:userCalc3.A", 42.0).allowed is False


def test_default_max_array_points_matches_ca_client():
    """policy.py deliberately duplicates this constant instead of importing
    it, to keep its stdlib+yaml-only import list (PLAN.md 5). This pins the
    two together so the duplication cannot drift."""
    from epics_mcp import ca_client
    from epics_mcp import policy as policy_mod

    assert policy_mod.DEFAULT_MAX_ARRAY_POINTS == ca_client.DEFAULT_MAX_ARRAY_POINTS


def test_max_array_points_defaults_and_is_overridable(tmp_path):
    from epics_mcp.policy import DEFAULT_MAX_ARRAY_POINTS

    path = write_policy(tmp_path, base_policy())
    assert Policy.load(path).max_array_points == DEFAULT_MAX_ARRAY_POINTS

    path = write_policy(tmp_path, base_policy(max_array_points=7), name="custom.yaml")
    assert Policy.load(path).max_array_points == 7


def test_max_array_points_appears_in_describe(tmp_path):
    path = write_policy(tmp_path, base_policy(max_array_points=12))
    assert Policy.load(path).describe()["max_array_points"] == 12
