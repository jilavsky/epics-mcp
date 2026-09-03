"""Tests for `cli.py`: policy resolution, --check, doctor, exit codes.

`main()` is called directly (not via subprocess) for speed; the console
script itself, and a real stdio MCP session against it, were exercised
manually against both example policies during development -- see PLAN.md's
own description of the tool surface for the scenarios that covers.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from epics_mcp.cli import main, resolve_policy_path


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
        # Deliberately no exact-name allow rule: doctor's CA-connection
        # check short-circuits to "skipped" instead of touching a real
        # network, which keeps this test suite offline and fast.
        "allow": [{"pattern": "usx*"}],
        "deny": [],
        "writes": [],
    }
    data.update(overrides)
    return data


# --- resolve_policy_path -----------------------------------------------


def test_resolve_bare_name_uses_policy_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("EPICS_MCP_POLICY_DIR", str(tmp_path))
    assert resolve_policy_path("usaxs-user") == tmp_path / "usaxs-user.yaml"


def test_resolve_path_with_slash_is_literal(monkeypatch):
    monkeypatch.delenv("EPICS_MCP_POLICY_DIR", raising=False)
    assert resolve_policy_path("./some/dir/policy") == pathlib.Path("./some/dir/policy")


def test_resolve_yaml_suffix_is_literal(monkeypatch, tmp_path):
    monkeypatch.setenv("EPICS_MCP_POLICY_DIR", str(tmp_path))
    assert resolve_policy_path("custom.yaml") == pathlib.Path("custom.yaml")


def test_resolve_absolute_path_is_literal(monkeypatch, tmp_path):
    monkeypatch.setenv("EPICS_MCP_POLICY_DIR", str(tmp_path / "policies"))
    absolute = tmp_path / "elsewhere" / "policy.yaml"
    assert resolve_policy_path(str(absolute)) == absolute


def test_resolve_uses_default_policy_dir_when_env_unset(monkeypatch):
    monkeypatch.delenv("EPICS_MCP_POLICY_DIR", raising=False)
    assert resolve_policy_path("usaxs-user") == pathlib.Path(
        "~/.epics-mcp/policies/usaxs-user.yaml"
    ).expanduser()


# --- no policy given -----------------------------------------------------


def test_no_policy_and_no_env_exits_2_with_resolution_order(capsys, monkeypatch):
    monkeypatch.delenv("EPICS_MCP_POLICY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "no policy, no PVs" in err
    assert "EPICS_MCP_POLICY_DIR" in err


def test_policy_from_env_var_is_used(monkeypatch, tmp_path, capsys):
    path = write_policy(tmp_path, base_policy())
    monkeypatch.setenv("EPICS_MCP_POLICY", str(path))
    rc = main(["--check"])
    assert rc == 0
    described = json.loads(capsys.readouterr().out)
    assert described["mode"] == "read-only"


# --- --check ---------------------------------------------------------------


def test_check_prints_effective_policy_and_exits_0(tmp_path, capsys):
    path = write_policy(tmp_path, base_policy(mode="read-write", writes=[
        {"pattern": "usx:foo", "range": [0, 10]}
    ]))
    rc = main(["--policy", str(path), "--check"])
    assert rc == 0
    described = json.loads(capsys.readouterr().out)
    assert described["mode"] == "read-write"
    assert described["writes"][0]["pattern"] == "usx:foo"


def test_readonly_flag_overrides_check_output(tmp_path, capsys):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
    )
    rc = main(["--policy", str(path), "--readonly", "--check"])
    assert rc == 0
    described = json.loads(capsys.readouterr().out)
    assert described["mode"] == "read-write"  # the file itself is unchanged
    assert described["effective_mode"] == "read-only"  # but the override wins
    assert described["force_readonly"] is True


def test_malformed_policy_prints_error_and_exits_2(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("mode: not-a-real-mode\nversion: 1\n")
    rc = main(["--policy", str(path), "--check"])
    assert rc == 2
    assert "mode" in capsys.readouterr().err


def test_missing_policy_file_prints_error_and_exits_2(tmp_path, capsys):
    rc = main(["--policy", str(tmp_path / "does_not_exist.yaml"), "--check"])
    assert rc == 2
    assert "does_not_exist.yaml" in capsys.readouterr().err


def test_warnings_are_printed_to_stderr(tmp_path, capsys):
    path = write_policy(tmp_path, base_policy())  # no audit: block -> a warning
    main(["--policy", str(path), "--check"])
    assert "warning" in capsys.readouterr().err.lower()


# --- doctor ------------------------------------------------------------


def test_doctor_reports_ok_for_policy_catalog_and_audit(tmp_path, capsys):
    """The overall exit code is 1 here, and correctly so: `base_policy` has
    no exact-name allow rule, so the ca_connection check has nothing to
    test against and reports "skipped" -- which counts as not-ok, matching
    aievaluator/doctor.py's own convention that a skipped check is not a
    pass. Policy/catalog/audit are what this test actually verifies."""
    path = write_policy(
        tmp_path,
        base_policy(
            catalog=str(tmp_path / "cat.txt"),
            audit={"path": str(tmp_path / "audit.log")},
        ),
    )
    (tmp_path / "cat.txt").write_text("usx:foo   a thing\n")
    rc = main(["doctor", "--policy", str(path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[OK] policy" in out
    assert "[OK] catalog" in out
    assert "[OK] audit" in out
    assert "ca_connection" in out


def test_doctor_bad_policy_exits_1(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("mode: nonsense\nversion: 1\n")
    rc = main(["doctor", "--policy", str(path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] policy" in out


def test_doctor_missing_catalog_reports_fail_but_continues(tmp_path, capsys):
    path = write_policy(tmp_path, base_policy(catalog="does_not_exist.txt"))
    rc = main(["doctor", "--policy", str(path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] catalog" in out
    # policy itself was fine, so later checks still ran
    assert "pyepics" in out


def test_doctor_missing_policy_file_exits_1(tmp_path, capsys):
    rc = main(["doctor", "--policy", str(tmp_path / "nope.yaml")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] policy" in out


# --- the shipped examples, through the CLI ------------------------------


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_check_against_shipped_readonly_example(capsys):
    example = REPO_ROOT / "examples" / "policy_usaxs_readonly.yaml"
    rc = main(["--policy", str(example), "--check"])
    assert rc == 0
    described = json.loads(capsys.readouterr().out)
    assert described["mode"] == "read-only"


@pytest.mark.ioc
def test_doctor_against_shipped_readonly_example(capsys):
    """The shipped example has exact-name allow rules (XFD:srCurrent, ...),
    so `check_representative_pv` attempts a real Channel Access connection
    -- marked `ioc` and deselected by default (see pyproject.toml's addopts
    and tests/README.md) so the default suite never opens a CA socket."""
    example = REPO_ROOT / "examples" / "policy_usaxs_readonly.yaml"
    rc = main(["doctor", "--policy", str(example)])
    out = capsys.readouterr().out
    assert "[OK] policy" in out
    assert "[OK] catalog" in out
    assert "audit" in out
    assert rc in (0, 1)
