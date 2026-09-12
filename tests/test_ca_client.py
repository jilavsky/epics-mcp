"""Tests for `ca_client.py`, entirely against `FakeBackend`.

Nothing here may import `epics` or open a socket -- see `tests/README.md`.
`PyepicsBackend` is only import-checked (lazy import guard), never
instantiated with real CA traffic outside `@pytest.mark.ioc` tests.
"""

from __future__ import annotations

import json

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
    assert set(reading.keys()) == set(PvReading.__annotations__)


def test_empty_reading_fills_every_declared_field():
    """An error reading must carry the same keys as a good one -- a client
    that indexes `reading["count"]` should not crash on a dead PV."""
    reading = empty_reading("usx:foo", error="boom")
    for key in PvReading.__annotations__:
        assert key in reading, f"empty_reading is missing {key}"


def test_pyepics_backend_import_is_lazy():
    """Constructing/importing the module must not require pyepics to be
    usable; only *calling* a method that touches CA should need it."""
    from epics_mcp import ca_client

    assert ca_client.PyepicsBackend is not None


def test_pvreading_is_a_superset_of_the_aievaluator_shape():
    """PLAN.md 1.1: our PvReading must keep every field aievaluator defines.

    Originally this asserted exact equality. It was relaxed to a superset
    when array/string/enum support landed: `count`, `truncated` and
    `enum_string` have no counterpart in aievaluator's fixed scalar checks,
    but nothing aievaluator reads may go missing, which is the half of the
    invariant that actually protects interoperability.
    """
    aievaluator_io = pytest.importorskip("aievaluator.epics_io")
    ours = set(PvReading.__annotations__)
    theirs = set(aievaluator_io.PvReading.__annotations__)
    missing = theirs - ours
    assert not missing, f"PvReading dropped aievaluator fields: {missing}"


def test_pvreading_extra_fields_are_the_documented_ones():
    """Pin the superset so a field cannot be added without a deliberate edit."""
    aievaluator_io = pytest.importorskip("aievaluator.epics_io")
    extra = set(PvReading.__annotations__) - set(aievaluator_io.PvReading.__annotations__)
    assert extra == {"count", "truncated", "enum_string"}


# --- value coercion: strings, waveforms, arrays, enums (the reported bug) ---


def test_char_waveform_decodes_to_string():
    """The reported bug: usxLAX:userDir is a CHAR waveform holding a path,
    and came back as an array of integers instead of the string."""
    path = "/share1/USAXS_data/2026-09/09_12_Randy"
    raw = [ord(c) for c in path] + [0]  # NUL-terminated, as EPICS stores it
    backend = FakeBackend(values={"usx:userDir": raw}, field_types={"usx:userDir": "time_char"})
    reading = backend.get("usx:userDir", timeout=1.0)
    assert reading["value"] == path
    assert reading["count"] == len(raw)
    assert reading["truncated"] is False


def test_char_waveform_stops_at_first_nul():
    """Trailing garbage after the NUL is not part of the string."""
    raw = [ord("o"), ord("k"), 0, 88, 89, 90]
    backend = FakeBackend(values={"p": raw}, field_types={"p": "time_char"})
    assert backend.get("p", timeout=1.0)["value"] == "ok"


def test_char_waveform_handles_signed_bytes():
    """EPICS CHAR can be signed; a high byte may arrive as a negative int."""
    raw = [-61, -87, 0]  # UTF-8 for 'é' as signed chars
    backend = FakeBackend(values={"p": raw}, field_types={"p": "time_char"})
    assert backend.get("p", timeout=1.0)["value"] == "é"


def test_char_waveform_is_not_guessed_without_the_field_type():
    """An int array with no CHAR field type stays an array -- we never guess
    that small integers 'look like' text."""
    backend = FakeBackend(values={"p": [72, 105]})
    assert backend.get("p", timeout=1.0)["value"] == [72, 105]


def test_numeric_array_is_truncated_with_true_count_reported():
    backend = FakeBackend(values={"p": list(range(8000))}, max_array_points=100)
    reading = backend.get("p", timeout=1.0)
    assert reading["value"] == list(range(100))
    assert reading["count"] == 8000
    assert reading["truncated"] is True


def test_short_array_is_not_marked_truncated():
    backend = FakeBackend(values={"p": [1.0, 2.0, 3.0]}, max_array_points=100)
    reading = backend.get("p", timeout=1.0)
    assert reading["value"] == [1.0, 2.0, 3.0]
    assert reading["count"] == 3
    assert reading["truncated"] is False


def test_enum_value_carries_its_label():
    backend = FakeBackend(
        values={"p": 0},
        field_types={"p": "time_enum"},
        enum_strings={"p": ("Passive", "Event", "I/O Intr")},
    )
    reading = backend.get("p", timeout=1.0)
    assert reading["value"] == 0
    assert reading["enum_string"] == "Passive"


def test_enum_out_of_range_index_does_not_fail_the_read():
    backend = FakeBackend(
        values={"p": 99}, field_types={"p": "time_enum"}, enum_strings={"p": ("A", "B")}
    )
    reading = backend.get("p", timeout=1.0)
    assert reading["value"] == 99
    assert reading["enum_string"] is None


def test_non_enum_pv_has_no_enum_string():
    backend = FakeBackend(values={"p": 1.5})
    assert backend.get("p", timeout=1.0)["enum_string"] is None


def test_plain_string_pv_passes_through():
    backend = FakeBackend(values={"p": "Tension - keep LoadFrame"})
    reading = backend.get("p", timeout=1.0)
    assert reading["value"] == "Tension - keep LoadFrame"
    assert reading["count"] == 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_become_null(bad):
    """json.dumps renders these as bare NaN/Infinity, which is invalid JSON
    and breaks strict client parsers for the whole response."""
    backend = FakeBackend(values={"p": bad})
    assert backend.get("p", timeout=1.0)["value"] is None


def test_non_finite_floats_inside_an_array_become_null():
    backend = FakeBackend(values={"p": [1.0, float("nan"), 3.0]})
    assert backend.get("p", timeout=1.0)["value"] == [1.0, None, 3.0]


def test_bytes_value_decodes_to_string():
    backend = FakeBackend(values={"p": b"hello\x00padding"})
    assert backend.get("p", timeout=1.0)["value"] == "hello"


def test_every_reading_is_json_serializable():
    """The whole point: a reading must survive json.dumps, because the MCP
    layer serializes it straight to the client."""
    backend = FakeBackend(
        values={
            "scalar": 1.5,
            "array": list(range(500)),
            "text": "hello",
            "chars": [ord("h"), ord("i"), 0],
            "enum": 1,
            "nan": float("nan"),
        },
        field_types={"chars": "time_char", "enum": "time_enum"},
        enum_strings={"enum": ("zero", "one")},
    )
    for name in ["scalar", "array", "text", "chars", "enum", "nan", "missing"]:
        json.dumps(backend.get(name, timeout=1.0))  # must not raise
