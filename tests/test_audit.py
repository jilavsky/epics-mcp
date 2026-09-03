"""Tests for the audit log (PLAN.md 4.7 / 7)."""

from __future__ import annotations

import json

from epics_mcp.audit import AuditLog, iter_records


def test_record_writes_one_json_line(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record("get", pv="usx:foo", value=1.5)
    lines = (tmp_path / "audit.log").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "get"
    assert record["pv"] == "usx:foo"
    assert record["value"] == 1.5
    assert "ts" in record


def test_record_creates_parent_directory(tmp_path):
    log = AuditLog(tmp_path / "nested" / "dir" / "audit.log")
    log.record("get", pv="usx:foo")
    assert (tmp_path / "nested" / "dir" / "audit.log").exists()


def test_multiple_records_append_in_order(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record("get", pv="a")
    log.record("get", pv="b")
    log.record("get", pv="c")
    records = list(iter_records(tmp_path / "audit.log"))
    assert [r["pv"] for r in records] == ["a", "b", "c"]


def test_intent_then_outcome_pairing_survives_a_write_that_raises(tmp_path):
    """A write that hangs or raises after the intent record must still
    leave a trace -- the intent record itself (PLAN.md 4.7)."""
    log = AuditLog(tmp_path / "audit.log")
    log.record("put", phase="intent", pv="usx:foo", old=1.0, new=2.0)
    try:
        raise RuntimeError("simulated CA timeout")
    except RuntimeError as exc:
        log.record("put", phase="outcome", pv="usx:foo", ok=False, error=str(exc))
    records = list(iter_records(tmp_path / "audit.log"))
    assert records[0]["phase"] == "intent"
    assert records[1]["phase"] == "outcome"
    assert records[1]["ok"] is False


def test_policy_sha256_field_round_trips(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record("put", pv="usx:foo", policy_sha256="abc123")
    record = next(iter_records(tmp_path / "audit.log"))
    assert record["policy_sha256"] == "abc123"


def test_iter_records_on_missing_file_yields_nothing(tmp_path):
    assert list(iter_records(tmp_path / "does_not_exist.log")) == []


def test_rotation_moves_current_file_to_dot_one(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, max_bytes=200, keep=3)
    for i in range(30):
        log.record("get", pv=f"usx:pv{i}", note="padding" * 3)
    assert path.exists()
    assert (tmp_path / "audit.log.1").exists()


def test_rotation_respects_keep_limit(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, max_bytes=150, keep=2)
    for i in range(60):
        log.record("get", pv=f"usx:pv{i}", note="padding" * 3)
    # Only .1 and .2 should exist -- keep=2 means at most 2 rotated files
    # plus the live one.
    assert path.exists()
    assert (tmp_path / "audit.log.1").exists()
    assert (tmp_path / "audit.log.2").exists()
    assert not (tmp_path / "audit.log.3").exists()


def test_no_line_is_split_across_rotation(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, max_bytes=100, keep=4)
    for i in range(80):
        log.record("get", pv=f"usx:pv{i}")
    for candidate in [path] + [tmp_path / f"audit.log.{n}" for n in (1, 2, 3, 4)]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                json.loads(line)  # must not raise
