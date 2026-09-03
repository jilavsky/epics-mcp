"""Append-only JSON Lines audit trail (PLAN.md 4.7).

The server is the only component that reliably knows timestamp, PV, old
value, new value and client identity together for a gated operation --
that is why the log lives here, not in a client like AIDA.

`AuditLog` itself does not decide *what* to log -- `policy.AuditConfig`'s
`log_reads` / `log_denials` flags are consulted by the caller (`server.py`);
this module only knows how to write a record safely and rotate the file.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_KEEP = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AuditLog:
    """One JSON object per line, one line per `record()` call.

    Every write is flushed and fsync'd immediately -- an audit record lost
    to an OS write cache on crash defeats the point. Rotation is size-based
    and happens before a write that would exceed `max_bytes`, never
    mid-write, so no line is ever split across files.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int | None = None,
        keep: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes or DEFAULT_MAX_BYTES
        self.keep = keep or DEFAULT_KEEP
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> None:
        line = json.dumps({"ts": _now_iso(), "event": event, **fields}, default=str) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with self.path.open("ab") as f:
                f.write(encoded)
                f.flush()
                _fsync_quietly(f)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= self.max_bytes:
            return
        # Shift .(keep-1) -> .keep, ..., .1 -> .2, oldest falls off the end.
        for i in range(self.keep - 1, 0, -1):
            src = _numbered(self.path, i)
            dst = _numbered(self.path, i + 1)
            if src.exists():
                src.replace(dst)
        self.path.replace(_numbered(self.path, 1))


def _numbered(path: Path, n: int) -> Path:
    return path.with_name(path.name + f".{n}")


def _fsync_quietly(f) -> None:
    try:
        import os

        os.fsync(f.fileno())
    except OSError:
        # Some filesystems (notably certain network mounts) refuse fsync;
        # the flush() above already pushed the write out of Python's buffer.
        pass


def iter_records(path: str | Path):
    """Yield parsed JSON records from an audit file, in file order.

    A convenience for tests and for anyone inspecting a log by hand; the
    server itself never reads its own audit log back.
    """
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
