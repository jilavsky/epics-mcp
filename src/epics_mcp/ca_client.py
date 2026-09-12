"""Channel Access behind one small interface.

The only module in this package that imports ``epics`` -- and it does so
lazily, on first use, exactly like ``aievaluator/epics_io.py`` does and for
the same reason: policy loading, the CLI's ``--help``/``--check``, and the
default test suite must all work on a machine with no EPICS installed
(PLAN.md 5.2).

``PvReading`` is a *superset* of ``aievaluator.epics_io``'s ``PvReading``
(PLAN.md 1.1) -- every field aievaluator defines is present with the same
name and meaning, plus ``count``/``truncated``/``enum_string``, which
aievaluator's fixed scalar checks never needed.
``tests/test_ca_client.py`` checks the superset relation by reflection when
aievaluator happens to be importable. The two implementations are otherwise
independent; this package must never import aievaluator (PLAN.md 1).

**Every value leaving this module must be JSON-safe** -- the MCP layer
serializes readings straight to the client, and a raw pyepics value is
frequently not. See :func:`coerce_ca_value`.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol, TypedDict

logger = logging.getLogger(__name__)

_EPICS_IMPORT_ERROR = "pyepics not installed. `pip install epics-mcp[dev]` or `conda install -c conda-forge pyepics`."

# Arrays are truncated to this many elements unless a policy says otherwise.
# An 8000-point sscan waveform (usxLAX:scan1.P1PA is exactly that) is not
# something an LLM should ever receive in full: it is ~100 kB of digits that
# crowds out the rest of the conversation and answers no question that the
# first hundred points plus the true `count` do not.
DEFAULT_MAX_ARRAY_POINTS = 100

# Decoded char-waveform strings are capped here. EPICS long strings are
# conventionally <= 4096, so this only ever trips on something pathological.
MAX_STRING_CHARS = 8192

# pyepics field-type names, as reported by `PV.type`, grouped by how the
# value has to be interpreted. Each appears bare and with the time_/ctrl_
# prefixes pyepics uses depending on how the channel was requested.
CHAR_FIELD_TYPES = frozenset({"char", "time_char", "ctrl_char"})
ENUM_FIELD_TYPES = frozenset({"enum", "time_enum", "ctrl_enum"})


class PvReading(TypedDict):
    pv: str
    value: Any
    units: str | None
    connected: bool
    timestamp: float | None
    severity: int | None
    status: int | None
    error: str | None
    # --- beyond aievaluator's shape; see the module docstring ---
    # Element count as EPICS reports it: 1 for a scalar, NELM for a
    # waveform. Stays the TRUE count even when `value` was truncated.
    count: int
    # True when `value` holds fewer elements/characters than `count`.
    truncated: bool
    # For enum (mbbi/bo/...) records: the label for `value`, e.g. "Passive".
    # None for every other field type.
    enum_string: str | None


def empty_reading(pv_name: str, *, error: str | None = None) -> PvReading:
    return {
        "pv": pv_name,
        "value": None,
        "units": None,
        "connected": False,
        "timestamp": None,
        "severity": None,
        "status": None,
        "error": error,
        "count": 0,
        "truncated": False,
        "enum_string": None,
    }


class CoercedValue(NamedTuple):
    value: Any
    count: int
    truncated: bool


def _char_array_to_str(elements: list) -> str:
    """Decode an EPICS CHAR waveform into the string it is holding.

    EPICS has no long-string type, so anything longer than 40 characters --
    a directory path, a sample title, a status message -- is conventionally
    stored as a NUL-terminated CHAR waveform. `caget -S` and pyepics'
    `as_string=True` both decode it this way, and so must we: returning the
    raw uint8 array would be technically faithful and practically useless
    (the reported bug: `usxLAX:userDir` came back as an array of 39 integers
    instead of "/share1/USAXS_data/2026-09/09_12_Randy").

    Decoding stops at the first NUL. CHAR can be signed in EPICS, so
    negative values are folded back into 0-255 before decoding.
    """
    out = bytearray()
    for raw in elements:
        try:
            code = int(raw) & 0xFF
        except (TypeError, ValueError):
            break
        if code == 0:
            break
        out.append(code)
    return out.decode("utf-8", errors="replace")


def _to_python(value: Any) -> Any:
    """numpy scalar -> Python scalar, numpy array -> list, everything else as-is.

    `tolist()` covers both cases (on a 0-d array or a numpy scalar it
    returns a plain Python number), which avoids importing numpy here just
    to isinstance-check against it.
    """
    tolist = getattr(value, "tolist", None)
    return tolist() if callable(tolist) else value


def _json_safe_number(value: Any) -> Any:
    """Replace NaN/+-Inf with None.

    `json.dumps` renders these as the bare literals `NaN` / `Infinity`,
    which are not valid JSON and are rejected by strict parsers -- so a
    single undefined reading could make a whole tool response unparseable
    for the client. A null reads correctly as "no usable value" instead.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def coerce_ca_value(
    raw: Any,
    *,
    field_type: str | None = None,
    max_array_points: int = DEFAULT_MAX_ARRAY_POINTS,
) -> CoercedValue:
    """Turn a raw Channel Access value into something JSON-serializable.

    Handles, in order: CHAR waveforms (-> str), numpy scalars and arrays
    (-> Python scalars and lists, truncated to `max_array_points`), bytes
    (-> str), and non-finite floats (-> None). `field_type` is pyepics'
    `PV.type`; when it is unknown, a list/array of small integers is *not*
    guessed to be a string -- only an explicit CHAR type triggers that.
    """
    field_type = field_type or ""

    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        return _truncate_str(text, len(text))

    native = _to_python(raw)

    if isinstance(native, list):
        full_count = len(native)
        if field_type in CHAR_FIELD_TYPES:
            return _truncate_str(_char_array_to_str(native), full_count)
        clipped = native[:max_array_points]
        return CoercedValue(
            value=[_json_safe_number(v) for v in clipped],
            count=full_count,
            truncated=len(clipped) < full_count,
        )

    if isinstance(native, str):
        return _truncate_str(native, 1)

    return CoercedValue(value=_json_safe_number(native), count=1, truncated=False)


def _truncate_str(text: str, count: int) -> CoercedValue:
    if len(text) > MAX_STRING_CHARS:
        return CoercedValue(value=text[:MAX_STRING_CHARS], count=count, truncated=True)
    return CoercedValue(value=text, count=count, truncated=False)


def _enum_label(labels: tuple[str, ...] | None, value: Any) -> str | None:
    """The label for an enum value, or None if it cannot be resolved.

    An out-of-range index is not an error worth failing a read over -- it
    just means the IOC and its ctrlvars disagree, so the numeric value
    stands on its own.
    """
    if not labels or not isinstance(value, int) or isinstance(value, bool):
        return None
    if 0 <= value < len(labels):
        return labels[value]
    return None


class PvInfo(TypedDict, total=False):
    pv: str
    units: str | None
    precision: int | None
    lower_disp_limit: float | None
    upper_disp_limit: float | None
    lower_ctrl_limit: float | None
    upper_ctrl_limit: float | None
    enum_strings: tuple[str, ...] | None
    # pyepics' `PV.type`: the CA *field* type ("time_double", "time_char",
    # "time_enum", ...). Previously mislabeled `record_type`, which it never
    # was -- the record type (ai, motor, waveform) lives in the .RTYP field
    # and would cost an extra CA round trip to fetch.
    field_type: str | None
    # Element count: 1 for a scalar, NELM for a waveform. Tells the model
    # whether `epics_pv_get` will return a truncated array.
    count: int | None
    description: str | None
    connected: bool
    error: str | None


class CaBackend(Protocol):
    """What `server.py` needs from Channel Access. Never raises for an
    instrument condition -- a dead IOC is a `connected: false` reading, not
    an exception (PLAN.md 3)."""

    def get(self, pv: str, timeout: float) -> PvReading: ...

    def get_many(self, pvs: list[str], timeout: float) -> list[PvReading]:
        return [self.get(pv, timeout) for pv in pvs]

    def info(self, pv: str, timeout: float) -> PvInfo: ...

    def put(self, pv: str, value: Any, timeout: float) -> PvReading: ...

    def monitor(
        self, pvs: list[str], seconds: float, interval: float
    ) -> dict[str, list[tuple[float, Any]]]: ...


# --------------------------------------------------------------------------
# Fake backend -- the only backend the default test suite uses, and a
# reasonable way to demo/develop this server off the beamline network.
# --------------------------------------------------------------------------


@dataclass
class FakeBackend:
    """Canned Channel Access for tests and offline development.

    `values` maps PV name to its current value; reads reflect the current
    map, `put` mutates it (so a `pv_get` after a `pv_put` sees the new
    value, the way a real IOC would). `disconnected` and `errors` let a
    test simulate the two failure shapes `server.py` must handle without
    raising.
    """

    values: dict[str, Any] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    disconnected: set[str] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)
    info_overrides: dict[str, PvInfo] = field(default_factory=dict)
    # PV name -> pyepics field-type name ("time_char", "time_enum", ...), so
    # a test can simulate a CHAR waveform or an enum without a real IOC.
    field_types: dict[str, str] = field(default_factory=dict)
    # PV name -> enum labels, for simulating enum records.
    enum_strings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_array_points: int = DEFAULT_MAX_ARRAY_POINTS
    sleep_fn: Any = time.sleep

    puts: list[tuple[str, Any]] = field(default_factory=list, init=False, repr=False)

    def get(self, pv: str, timeout: float) -> PvReading:
        if pv in self.errors:
            return empty_reading(pv, error=self.errors[pv])
        if pv in self.disconnected or pv not in self.values:
            return empty_reading(pv, error=f"Could not connect to PV '{pv}' within {timeout}s")
        field_type = self.field_types.get(pv, "")
        coerced = coerce_ca_value(
            self.values[pv], field_type=field_type, max_array_points=self.max_array_points
        )
        return {
            "pv": pv,
            "value": coerced.value,
            "units": self.units.get(pv),
            "connected": True,
            "timestamp": time.time(),
            "severity": 0,
            "status": 0,
            "error": None,
            "count": coerced.count,
            "truncated": coerced.truncated,
            "enum_string": _enum_label(self.enum_strings.get(pv), coerced.value)
            if field_type in ENUM_FIELD_TYPES
            else None,
        }

    def get_many(self, pvs: list[str], timeout: float) -> list[PvReading]:
        return [self.get(pv, timeout) for pv in pvs]

    def info(self, pv: str, timeout: float) -> PvInfo:
        if pv in self.info_overrides:
            return self.info_overrides[pv]
        reading = self.get(pv, timeout)
        if not reading["connected"]:
            return {"pv": pv, "connected": False, "error": reading["error"]}
        return {
            "pv": pv,
            "connected": True,
            "units": reading["units"],
            "precision": None,
            "lower_disp_limit": None,
            "upper_disp_limit": None,
            "lower_ctrl_limit": None,
            "upper_ctrl_limit": None,
            "enum_strings": self.enum_strings.get(pv),
            "field_type": self.field_types.get(pv) or None,
            "count": reading["count"],
            "description": None,
            "error": None,
        }

    def put(self, pv: str, value: Any, timeout: float) -> PvReading:
        if pv in self.errors:
            return empty_reading(pv, error=self.errors[pv])
        if pv in self.disconnected:
            return empty_reading(pv, error=f"Could not connect to PV '{pv}' within {timeout}s")
        self.values[pv] = value
        self.puts.append((pv, value))
        return self.get(pv, timeout)

    def monitor(
        self, pvs: list[str], seconds: float, interval: float
    ) -> dict[str, list[tuple[float, Any]]]:
        samples: dict[str, list[tuple[float, Any]]] = {pv: [] for pv in pvs}
        start = time.time()
        # Counting samples rather than accumulating `elapsed += interval`
        # sidesteps float drift (0.1 * 3 != 0.3) that would otherwise drop
        # or add a boundary sample depending on rounding.
        num_samples = int(round(seconds / interval)) + 1
        for i in range(num_samples):
            elapsed = i * interval
            for pv in pvs:
                reading = self.get(pv, interval)
                samples[pv].append((start + elapsed, reading["value"]))
            if i < num_samples - 1:
                self.sleep_fn(interval)
        return samples


# --------------------------------------------------------------------------
# pyepics backend -- lazy import, cached PV objects, never raises.
# --------------------------------------------------------------------------

_epics_module = None
_logged_ca_env = False


def _get_epics():
    global _epics_module
    if _epics_module is None:
        try:
            import epics
        except ImportError as exc:
            raise RuntimeError(_EPICS_IMPORT_ERROR) from exc
        _epics_module = epics
    return _epics_module


def _log_ca_env_once() -> None:
    global _logged_ca_env
    if _logged_ca_env:
        return
    _logged_ca_env = True
    logger.info(
        "EPICS_CA_ADDR_LIST=%r EPICS_CA_AUTO_ADDR_LIST=%r",
        os.environ.get("EPICS_CA_ADDR_LIST", ""),
        os.environ.get("EPICS_CA_AUTO_ADDR_LIST", ""),
    )


class PyepicsBackend:
    """Real Channel Access, via pyepics. Caches PV objects across calls."""

    def __init__(self, max_array_points: int = DEFAULT_MAX_ARRAY_POINTS) -> None:
        self._pvs: dict[str, Any] = {}
        self.max_array_points = max_array_points
        _log_ca_env_once()

    def _pv(self, pv_name: str):
        epics = _get_epics()
        if pv_name not in self._pvs:
            self._pvs[pv_name] = epics.PV(pv_name, auto_monitor=False)
        return self._pvs[pv_name]

    @staticmethod
    def _enum_strings(handle) -> tuple[str, ...] | None:
        """Enum labels for a PV, or None.

        Touching `enum_strs` makes pyepics fetch ctrlvars if it has not
        already, but it caches them on the PV object, so this costs one
        extra CA round trip per PV per process -- not one per read.
        """
        try:
            labels = getattr(handle, "enum_strs", None)
        except Exception:  # noqa: BLE001 -- metadata is never worth failing a read over
            return None
        return tuple(labels) if labels else None

    def get(self, pv: str, timeout: float) -> PvReading:
        try:
            handle = self._pv(pv)
            if not handle.wait_for_connection(timeout=timeout):
                return empty_reading(pv, error=f"Could not connect to PV '{pv}' within {timeout}s")
            raw = handle.get(timeout=timeout)
            if raw is None:
                # Connected but the get itself timed out or returned nothing.
                # Reported as a reading, not raised (PLAN.md 3).
                return empty_reading(pv, error=f"Read of '{pv}' returned no value within {timeout}s")

            field_type = handle.type or ""
            coerced = coerce_ca_value(
                raw, field_type=field_type, max_array_points=self.max_array_points
            )
            enum_string = (
                _enum_label(self._enum_strings(handle), coerced.value)
                if field_type in ENUM_FIELD_TYPES
                else None
            )
            return {
                "pv": pv,
                "value": coerced.value,
                "units": handle.units,
                "connected": True,
                "timestamp": handle.timestamp,
                "severity": handle.severity,
                "status": handle.status,
                "error": None,
                # `handle.count` is the channel's element count; fall back to
                # what the value itself carried if CA did not report one.
                "count": int(handle.count or coerced.count),
                "truncated": coerced.truncated,
                "enum_string": enum_string,
            }
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 -- PLAN.md 3: never raise for an instrument condition
            return empty_reading(pv, error=str(exc))

    def get_many(self, pvs: list[str], timeout: float) -> list[PvReading]:
        return [self.get(pv, timeout) for pv in pvs]

    def info(self, pv: str, timeout: float) -> PvInfo:
        try:
            handle = self._pv(pv)
            if not handle.wait_for_connection(timeout=timeout):
                return {"pv": pv, "connected": False, "error": f"Could not connect to PV '{pv}' within {timeout}s"}
            handle.get(timeout=timeout, as_string=False)
            return {
                "pv": pv,
                "connected": True,
                "units": handle.units,
                "precision": handle.precision,
                "lower_disp_limit": _json_safe_number(handle.lower_disp_limit),
                "upper_disp_limit": _json_safe_number(handle.upper_disp_limit),
                "lower_ctrl_limit": _json_safe_number(handle.lower_ctrl_limit),
                "upper_ctrl_limit": _json_safe_number(handle.upper_ctrl_limit),
                "enum_strings": self._enum_strings(handle),
                "field_type": handle.type,
                "count": int(handle.count) if handle.count else None,
                "description": _get_epics().caget(f"{pv}.DESC", timeout=timeout),
                "error": None,
            }
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"pv": pv, "connected": False, "error": str(exc)}

    def put(self, pv: str, value: Any, timeout: float) -> PvReading:
        try:
            handle = self._pv(pv)
            if not handle.wait_for_connection(timeout=timeout):
                return empty_reading(pv, error=f"Could not connect to PV '{pv}' within {timeout}s")
            handle.put(value, wait=True, timeout=timeout)
            return self.get(pv, timeout)
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            return empty_reading(pv, error=str(exc))

    def monitor(
        self, pvs: list[str], seconds: float, interval: float
    ) -> dict[str, list[tuple[float, Any]]]:
        samples: dict[str, list[tuple[float, Any]]] = {pv: [] for pv in pvs}
        start = time.time()
        num_samples = int(round(seconds / interval)) + 1
        for i in range(num_samples):
            elapsed = i * interval
            for pv in pvs:
                reading = self.get(pv, min(interval, 1.0))
                samples[pv].append((start + elapsed, reading["value"]))
            if i < num_samples - 1:
                time.sleep(interval)
        return samples

    def close(self) -> None:
        for handle in self._pvs.values():
            try:
                handle.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._pvs.clear()
