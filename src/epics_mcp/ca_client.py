"""Channel Access behind one small interface.

The only module in this package that imports ``epics`` -- and it does so
lazily, on first use, exactly like ``aievaluator/epics_io.py`` does and for
the same reason: policy loading, the CLI's ``--help``/``--check``, and the
default test suite must all work on a machine with no EPICS installed
(PLAN.md 5.2).

``PvReading`` is field-for-field identical to ``aievaluator.epics_io``'s
``PvReading`` on purpose (PLAN.md 1.1) -- ``tests/test_ca_client.py`` checks
this by reflection when aievaluator happens to be importable. The two
implementations are otherwise independent; this package must never import
aievaluator (PLAN.md 1).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

logger = logging.getLogger(__name__)

_EPICS_IMPORT_ERROR = "pyepics not installed. `pip install epics-mcp[dev]` or `conda install -c conda-forge pyepics`."


class PvReading(TypedDict):
    pv: str
    value: Any
    units: str | None
    connected: bool
    timestamp: float | None
    severity: int | None
    status: int | None
    error: str | None


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
    }


class PvInfo(TypedDict, total=False):
    pv: str
    units: str | None
    precision: int | None
    lower_disp_limit: float | None
    upper_disp_limit: float | None
    lower_ctrl_limit: float | None
    upper_ctrl_limit: float | None
    enum_strings: tuple[str, ...] | None
    record_type: str | None
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
    sleep_fn: Any = time.sleep

    puts: list[tuple[str, Any]] = field(default_factory=list, init=False, repr=False)

    def get(self, pv: str, timeout: float) -> PvReading:
        if pv in self.errors:
            return empty_reading(pv, error=self.errors[pv])
        if pv in self.disconnected or pv not in self.values:
            return empty_reading(pv, error=f"Could not connect to PV '{pv}' within {timeout}s")
        return {
            "pv": pv,
            "value": self.values[pv],
            "units": self.units.get(pv),
            "connected": True,
            "timestamp": time.time(),
            "severity": 0,
            "status": 0,
            "error": None,
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
            "enum_strings": None,
            "record_type": None,
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

    def __init__(self) -> None:
        self._pvs: dict[str, Any] = {}
        _log_ca_env_once()

    def _pv(self, pv_name: str):
        epics = _get_epics()
        if pv_name not in self._pvs:
            self._pvs[pv_name] = epics.PV(pv_name, auto_monitor=False)
        return self._pvs[pv_name]

    def get(self, pv: str, timeout: float) -> PvReading:
        try:
            handle = self._pv(pv)
            if not handle.wait_for_connection(timeout=timeout):
                return empty_reading(pv, error=f"Could not connect to PV '{pv}' within {timeout}s")
            value = handle.get(timeout=timeout)
            return {
                "pv": pv,
                "value": value,
                "units": handle.units,
                "connected": True,
                "timestamp": handle.timestamp,
                "severity": handle.severity,
                "status": handle.status,
                "error": None,
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
            enum_strs = tuple(handle.enum_strs) if getattr(handle, "enum_strs", None) else None
            return {
                "pv": pv,
                "connected": True,
                "units": handle.units,
                "precision": handle.precision,
                "lower_disp_limit": handle.lower_disp_limit,
                "upper_disp_limit": handle.upper_disp_limit,
                "lower_ctrl_limit": handle.lower_ctrl_limit,
                "upper_ctrl_limit": handle.upper_ctrl_limit,
                "enum_strings": enum_strs,
                "record_type": handle.type,
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
