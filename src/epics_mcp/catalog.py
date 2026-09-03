"""A static PV name catalog, for `epics_pv_search`.

Channel Access has no name-search protocol (PLAN.md 4.4), so `epics_pv_search`
searches this file instead. It also gives the model human-readable names for
cryptic records. Listing a PV here does not grant access to it -- the policy
file is the only thing that does that; see `examples/pv_catalog_usaxs.txt`'s
own header.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CatalogEntry:
    pv: str
    description: str


def load_catalog(path: str | Path) -> tuple[CatalogEntry, ...]:
    """Parse a catalog file: `PV<whitespace>description` per line.

    Blank lines and lines starting with `#` are ignored. Duplicate PV names
    are kept in file order (last one wins on lookup, but `search` returns
    every match) -- a catalog is documentation, not a database with a
    uniqueness constraint.
    """
    path = Path(path)
    entries: list[CatalogEntry] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(maxsplit=1)
        pv = parts[0]
        description = parts[1].strip() if len(parts) > 1 else ""
        entries.append(CatalogEntry(pv=pv, description=description))
    return tuple(entries)


def search(entries: tuple[CatalogEntry, ...], pattern: str) -> list[CatalogEntry]:
    """Case-insensitive substring search over PV name and description.

    Not a glob or regex engine on purpose: this tool answers "what PV might
    I mean by 'guard slit'", not "match this exact pattern" -- that's what
    the policy's own glob/regex rules are for.
    """
    needle = pattern.strip().lower()
    if not needle:
        return []
    return [
        entry
        for entry in entries
        if needle in entry.pv.lower() or needle in entry.description.lower()
    ]
