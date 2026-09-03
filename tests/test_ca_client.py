"""Tests for `ca_client.py`, entirely against `FakeBackend`.

Nothing here may import `epics` or open a socket -- see `tests/README.md`.
`PyepicsBackend` is only import-checked (lazy import guard), never
instantiated with real CA traffic outside `@pytest.mark.ioc` tests.
"""

from __future__ import annotations

import pytest

from epics_mcp.ca_client import FakeBackend, PvReading, empty_reading


def test_get_connected_pv():
    backend = FakeBackend(values={"usx:foo": 1.5}, units={"usx:foo": "mm"})
    reading = backend.get("usx:foo", timeout=1.0)
    assert reading["connected"] is True
    assert reading["value"] == 1.5
    assert reading["units"] == "mm"
    assert reading["error"] is None


def test_get_unknown_pv_is_disconnected_not_an_exception():
    backend = FakeBackend()
    reading = backend.get("nope:foo", timeout=1.0)
    assert reading["connected"] is False
    assert reading["value"] is None
    assert "Could not connect" in reading["error"]


def test_get_explicitly_disconnected_pv():
    backend = FakeBackend(values={"usx:foo": 1.0}, disconnected={"usx:foo"})
    reading = backend.get("usx:foo", timeout=1.0)
    assert reading["connected"] is False


def test_get_pv_with_forced_error():
    backend = FakeBackend(errors={"usx:foo": "simulated CA error"})
    reading = backend.get("usx:foo", timeout=1.0)
    assert reading["connected"] is False
    assert reading["error"] == "simulated CA error"


def test_get_many_preserves_order():
    backend = FakeBackend(values={"a": 1, "b": 2, "c": 3})
    readings = backend.get_many(["c", "a", "missing", "b"], timeout=1.0)
    assert [r["pv"] for r in readings] == ["c", "a", "missing", "b"]
    assert [r["connected"] for r in readings] == [True, True, False, True]


def test_put_then_get_reflects_new_value():
    backend = FakeBackend(values={"usx:foo": 1.0})
    put_reading = backend.put("usx:foo", 5.0, timeout=1.0)
    assert put_reading["connected"] is True
    assert put_reading["value"] == 5.0
    assert backend.get("usx:foo", timeout=1.0)["value"] == 5.0
    assert backend.puts == [("usx:foo", 5.0)]


def test_put_to_disconnected_pv_does_not_raise():
    backend = FakeBackend(disconnected={"usx:foo"})
    reading = backend.put("usx:foo", 5.0, timeout=1.0)
    assert reading["connected"] is False
    assert backend.puts == []


def test_info_for_connected_pv():
    backend = FakeBackend(values={"usx:foo": 1.0}, units={"usx:foo": "mm"})
    info = backend.info("usx:foo", timeout=1.0)
    assert info["connected"] is True
    assert info["units"] == "mm"


def test_info_for_disconnected_pv_has_no_crash():
    backend = FakeBackend()
    info = backend.info("nope:foo", timeout=1.0)
    assert info["connected"] is False
    assert info["error"]


def test_monitor_returns_bounded_series(monkeypatch):
    backend = FakeBackend(values={"usx:foo": 1.0}, sleep_fn=lambda _s: None)
    samples = backend.monitor(["usx:foo"], seconds=0.3, interval=0.1)
    assert "usx:foo" in samples
    assert len(samples["usx:foo"]) == 4  # t=0, 0.1, 0.2, 0.3 inclusive of the boundary
    assert all(value == 1.0 for _t, value in samples["usx:foo"])


def test_empty_reading_shape():
    reading: PvReading = empty_reading("usx:foo", error="boom")
    assert reading["pv"] == "usx:foo"
    assert reading["connected"] is False
    assert reading["error"] == "boom"
    assert set(reading.keys()) == {
        "pv", "value", "units", "connected", "timestamp", "severity", "status", "error",
    }


def test_pyepics_backend_import_is_lazy():
    """Constructing/importing the module must not require pyepics to be
    usable; only *calling* a method that touches CA should need it."""
    from epics_mcp import ca_client

    assert ca_client.PyepicsBackend is not None


def test_pvreading_matches_aievaluator_shape_when_aievaluator_is_importable():
    """PLAN.md 1.1: the two PvReading shapes must stay field-for-field
    identical, or the deliberate duplication has silently drifted."""
    aievaluator_io = pytest.importorskip("aievaluator.epics_io")
    ours = set(PvReading.__annotations__)
    theirs = set(aievaluator_io.PvReading.__annotations__)
    assert ours == theirs, f"PvReading fields diverged: ours={ours} theirs={theirs}"
