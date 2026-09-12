"""Tests for the FastMCP tool surface (PLAN.md 3 / 7), against `FakeBackend`.

`@mcp.tool()` (confirmed empirically) returns the original function object
unchanged, so most tests here call `server.epics_pv_get(...)` etc. directly
-- real Python objects in, real Python objects/exceptions out, no MCP
JSON-content round trip to reconstruct. That round trip (content blocks,
structured output, a raised exception becoming a protocol-level ToolError)
is FastMCP's own concern; `test_registration_and_wire_round_trip` below
exercises it once, end to end, via `mcp.call_tool` / `mcp.list_tools`, to
confirm this package's exceptions actually surface as tool errors to a real
client -- which is also covered by manual testing against a live stdio
session (see PLAN.md's own worked example).
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest
import yaml
from mcp.server.fastmcp.exceptions import ToolError

from epics_mcp import server
from epics_mcp.ca_client import FakeBackend
from epics_mcp.policy import Policy
from epics_mcp.server import PolicyDenied

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def write_policy(tmp_path: pathlib.Path, data: dict, name: str = "policy.yaml") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def base_policy(**overrides) -> dict:
    data = {
        "version": 1,
        "mode": "read-only",
        "default_timeout_s": 3.0,
        "max_pvs_per_call": 5,
        "max_watch_seconds": 2,
        "max_watch_samples": 10,
        "allow": [{"pattern": "usx*"}],
        "deny": [],
        "writes": [],
    }
    data.update(overrides)
    return data


def configure_with(tmp_path, policy_data, *, backend=None) -> FakeBackend:
    path = write_policy(tmp_path, policy_data)
    policy = Policy.load(path)
    fake = backend if backend is not None else FakeBackend()
    server.configure(policy, backend=fake, client_label="test")
    return fake


def tool_names() -> set[str]:
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name for t in tools}


# --- tool registration ---------------------------------------------------


def test_pv_put_absent_when_read_only(tmp_path):
    configure_with(tmp_path, base_policy(mode="read-only"))
    assert "epics_pv_put" not in tool_names()


def test_pv_put_present_when_read_write(tmp_path):
    configure_with(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
    )
    assert "epics_pv_put" in tool_names()


def test_pv_put_deregistered_on_reconfigure_to_read_only(tmp_path):
    configure_with(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    assert "epics_pv_put" in tool_names()
    configure_with(tmp_path, base_policy(mode="read-only"))
    assert "epics_pv_put" not in tool_names()


def test_readonly_override_hides_pv_put_even_in_readwrite_file(tmp_path):
    path = write_policy(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
    )
    policy = Policy.load(path, force_readonly=True)
    server.configure(policy, backend=FakeBackend())
    assert "epics_pv_put" not in tool_names()


# --- epics_pv_get ------------------------------------------------------------


def test_pv_get_returns_readings_in_order(tmp_path):
    configure_with(tmp_path, base_policy(), backend=FakeBackend(values={"usx:a": 1, "usx:b": 2}))
    readings = server.epics_pv_get(["usx:b", "usx:a"])
    assert [r["pv"] for r in readings] == ["usx:b", "usx:a"]
    assert [r["value"] for r in readings] == [2, 1]


def test_pv_get_disconnected_pv_is_not_an_exception(tmp_path):
    configure_with(tmp_path, base_policy())
    readings = server.epics_pv_get(["usx:nope"])
    assert readings[0]["connected"] is False
    assert readings[0]["error"]


def test_pv_get_denied_pv_raises_policy_denied(tmp_path):
    configure_with(tmp_path, base_policy(deny=[{"pattern": "usxSECRET:*"}]))
    with pytest.raises(PolicyDenied, match="usxSECRET:foo"):
        server.epics_pv_get(["usxSECRET:foo"])


def test_pv_get_over_max_pvs_per_call_raises(tmp_path):
    configure_with(tmp_path, base_policy(max_pvs_per_call=2))
    with pytest.raises(ValueError, match="max_pvs_per_call"):
        server.epics_pv_get(["usx:a", "usx:b", "usx:c"])


# --- epics_pv_info -----------------------------------------------------------


def test_pv_info_includes_writable_flag(tmp_path):
    configure_with(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    info = server.epics_pv_info("usx:foo")
    assert info["writable"] is True


def test_pv_info_not_writable_when_read_only(tmp_path):
    configure_with(tmp_path, base_policy(), backend=FakeBackend(values={"usx:foo": 1.0}))
    info = server.epics_pv_info("usx:foo")
    assert info["writable"] is False


def test_pv_info_denied_raises(tmp_path):
    configure_with(tmp_path, base_policy(deny=[{"pattern": "usxSECRET:*"}]))
    with pytest.raises(PolicyDenied, match="not readable"):
        server.epics_pv_info("usxSECRET:foo")


# --- epics_pv_watch ----------------------------------------------------------


def test_pv_watch_bounded_series(tmp_path):
    configure_with(
        tmp_path,
        base_policy(max_watch_seconds=1, max_watch_samples=100),
        backend=FakeBackend(values={"usx:foo": 1.0}, sleep_fn=lambda _s: None),
    )
    series = server.epics_pv_watch(["usx:foo"], seconds=0.2, interval=0.1)
    assert len(series["usx:foo"]) == 3
    assert all(value == 1.0 for _t, value in series["usx:foo"])


def test_pv_watch_exceeding_max_seconds_raises(tmp_path):
    configure_with(tmp_path, base_policy(max_watch_seconds=1))
    with pytest.raises(ValueError, match="max_watch_seconds"):
        server.epics_pv_watch(["usx:foo"], seconds=5.0, interval=1.0)


def test_pv_watch_exceeding_max_samples_raises(tmp_path):
    configure_with(tmp_path, base_policy(max_watch_seconds=100, max_watch_samples=3))
    with pytest.raises(ValueError, match="max_watch_samples"):
        server.epics_pv_watch(["usx:foo"], seconds=10.0, interval=1.0)


def test_pv_watch_denied_pv_raises(tmp_path):
    configure_with(tmp_path, base_policy(deny=[{"pattern": "usxSECRET:*"}]))
    with pytest.raises(PolicyDenied, match="denied by policy"):
        server.epics_pv_watch(["usxSECRET:foo"], seconds=0.1, interval=0.1)


# --- epics_policy_describe / epics_pv_search ---------------------------------


def test_policy_describe_returns_effective_policy(tmp_path):
    configure_with(tmp_path, base_policy())
    described = server.epics_policy_describe()
    assert described["mode"] == "read-only"
    assert described["backend"] == "FakeBackend"


def test_pv_search_reports_readable_and_writable(tmp_path):
    catalog_path = tmp_path / "cat.txt"
    catalog_path.write_text("usx:foo   a thing\nusxSECRET:bar   a secret thing\n")
    configure_with(
        tmp_path,
        base_policy(
            catalog="cat.txt",
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 10]}],
            deny=[{"pattern": "usxSECRET:*"}],
        ),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    hits = {h["pv"]: h for h in server.epics_pv_search("thing")}
    assert hits["usx:foo"]["readable"] is True
    assert hits["usx:foo"]["writable"] is True
    assert hits["usxSECRET:bar"]["readable"] is False
    assert hits["usxSECRET:bar"]["writable"] is False


# --- epics_pv_put ------------------------------------------------------------


def test_pv_put_simple_write(tmp_path):
    configure_with(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    payload = server.epics_pv_put("usx:foo", 5.0)
    assert payload == {"status": "ok", "pv": "usx:foo", "old_value": 1.0, "new_value": 5.0}


def test_pv_put_denied_raises(tmp_path):
    configure_with(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    with pytest.raises(PolicyDenied, match="not writable"):
        server.epics_pv_put("usx:bar", 5.0)


def test_pv_put_confirm_round_trip(tmp_path):
    fake = FakeBackend(values={"usx:foo": 1.0})
    configure_with(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 10], "confirm": True}],
        ),
        backend=fake,
    )
    challenge = server.epics_pv_put("usx:foo", 5.0)
    assert challenge["status"] == "confirm_required"
    assert challenge["current_value"] == 1.0
    assert fake.values["usx:foo"] == 1.0  # not yet written

    result = server.epics_pv_put("usx:foo", 5.0, confirm_token=challenge["token"])
    assert result["status"] == "ok"
    assert fake.values["usx:foo"] == 5.0


def test_pv_put_confirm_token_reused_is_invalid(tmp_path):
    configure_with(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 10], "confirm": True}],
        ),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    challenge = server.epics_pv_put("usx:foo", 5.0)
    token = challenge["token"]
    server.epics_pv_put("usx:foo", 5.0, confirm_token=token)
    result = server.epics_pv_put("usx:foo", 5.0, confirm_token=token)
    assert result["status"] == "confirm_invalid"


def test_pv_put_rate_limited(tmp_path):
    configure_with(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 100], "rate_limit_per_min": 1}],
        ),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    assert server.epics_pv_put("usx:foo", 5.0)["status"] == "ok"
    assert server.epics_pv_put("usx:foo", 6.0)["status"] == "rate_limited"


def test_pv_put_ca_failure_is_structured_not_raised(tmp_path):
    configure_with(
        tmp_path,
        base_policy(mode="read-write", writes=[{"pattern": "usx:foo", "range": [0, 10]}]),
        backend=FakeBackend(disconnected={"usx:foo"}),
    )
    payload = server.epics_pv_put("usx:foo", 5.0)
    assert payload["status"] == "error"
    assert payload["error"]


def test_pv_put_writes_intent_and_outcome_to_audit(tmp_path):
    configure_with(
        tmp_path,
        base_policy(
            mode="read-write",
            writes=[{"pattern": "usx:foo", "range": [0, 10]}],
            audit={"path": str(tmp_path / "audit.log")},
        ),
        backend=FakeBackend(values={"usx:foo": 1.0}),
    )
    server.epics_pv_put("usx:foo", 5.0)
    from epics_mcp.audit import iter_records

    records = list(iter_records(tmp_path / "audit.log"))
    phases = [r["phase"] for r in records if r["event"] == "put"]
    assert phases == ["intent", "outcome"]


def test_configure_requires_call_before_tool_use(monkeypatch):
    monkeypatch.setattr(server, "_state", None)
    with pytest.raises(RuntimeError, match="configure"):
        server._require_state()


# --- one real over-the-wire round trip ---------------------------------------


def test_registration_and_wire_round_trip(tmp_path):
    """A denial raised inside a tool function must surface as a protocol
    ToolError to an actual MCP client, not as a bare Python exception or a
    silently-swallowed failure -- confirmed here through `mcp.call_tool`,
    the same entry point a real client session uses."""
    configure_with(tmp_path, base_policy(deny=[{"pattern": "usxSECRET:*"}]))

    async def scenario():
        tools = await server.mcp.list_tools()
        assert "epics_pv_get" in {t.name for t in tools}
        assert "epics_pv_put" not in {t.name for t in tools}

        with pytest.raises(ToolError, match="usxSECRET:foo"):
            await server.mcp.call_tool("epics_pv_get", {"names": ["usxSECRET:foo"]})

    asyncio.run(scenario())


# --- value shapes through the tool layer (strings, waveforms, enums) --------


def test_pv_get_returns_char_waveform_as_a_string(tmp_path):
    """End to end for the reported bug: a CHAR waveform must reach the
    client as text, not as an array of integers."""
    path = "/share1/USAXS_data/2026-09/09_12_Randy"
    configure_with(
        tmp_path,
        base_policy(),
        backend=FakeBackend(
            values={"usx:userDir": [ord(c) for c in path] + [0]},
            field_types={"usx:userDir": "time_char"},
        ),
    )
    reading = server.epics_pv_get(["usx:userDir"])[0]
    assert reading["value"] == path


def test_pv_get_truncates_a_large_waveform_per_policy(tmp_path):
    configure_with(
        tmp_path,
        base_policy(max_array_points=10),
        backend=FakeBackend(values={"usx:wf": list(range(8000))}, max_array_points=10),
    )
    reading = server.epics_pv_get(["usx:wf"])[0]
    assert len(reading["value"]) == 10
    assert reading["count"] == 8000
    assert reading["truncated"] is True


def test_pv_get_output_is_json_serializable_for_every_value_shape(tmp_path):
    """The bug was a serialization failure, so assert serializability
    directly rather than only checking the Python-side values."""
    configure_with(
        tmp_path,
        base_policy(max_pvs_per_call=10),
        backend=FakeBackend(
            values={
                "usx:scalar": 1.5,
                "usx:text": "hello",
                "usx:chars": [ord("h"), ord("i"), 0],
                "usx:wf": list(range(500)),
                "usx:enum": 1,
                "usx:nan": float("nan"),
            },
            field_types={"usx:chars": "time_char", "usx:enum": "time_enum"},
            enum_strings={"usx:enum": ("zero", "one")},
        ),
    )
    readings = server.epics_pv_get(
        ["usx:scalar", "usx:text", "usx:chars", "usx:wf", "usx:enum", "usx:nan"]
    )
    json.dumps(readings)  # must not raise
    by_pv = {r["pv"]: r for r in readings}
    assert by_pv["usx:chars"]["value"] == "hi"
    assert by_pv["usx:enum"]["enum_string"] == "one"
    assert by_pv["usx:nan"]["value"] is None


def test_pv_info_reports_field_type_and_count(tmp_path):
    configure_with(
        tmp_path,
        base_policy(),
        backend=FakeBackend(
            values={"usx:wf": list(range(300))}, field_types={"usx:wf": "time_double"}
        ),
    )
    info = server.epics_pv_info("usx:wf")
    assert info["field_type"] == "time_double"
    assert info["count"] == 300
