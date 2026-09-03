"""Bare scaffolding checks: the package imports, examples/ is populated.

The safety properties of the shipped example policies used to be asserted
here as raw YAML, before `epics_mcp.policy` existed. They have moved to
`tests/test_policy.py` (the engine-level table) and `tests/test_examples.py`
(properties specific to `examples/`), where they exercise `Policy.load()`
instead of re-deriving policy semantics from a YAML dict by hand.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


def test_package_imports_and_reports_a_version():
    import epics_mcp

    assert epics_mcp.__version__


def test_examples_directory_is_populated():
    assert sorted(EXAMPLES.glob("policy_*.yaml")), "no example policies found"
    assert (EXAMPLES / "pv_catalog_usaxs.txt").is_file()


def test_pv_catalog_parses_and_every_entry_has_a_description():
    catalog = (EXAMPLES / "pv_catalog_usaxs.txt").read_text().splitlines()
    entries = 0
    for lineno, line in enumerate(catalog, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(maxsplit=1)
        assert len(parts) == 2, f"pv_catalog_usaxs.txt:{lineno} has no description"
        pv, description = parts
        assert ":" in pv, f"pv_catalog_usaxs.txt:{lineno}: {pv!r} is not a PV name"
        assert description.strip()
        entries += 1
    assert entries > 20, "catalog looks truncated"
