"""Tests for the PV catalog / `epics_pv_search` support (PLAN.md 4.4)."""

from __future__ import annotations

import pathlib

from epics_mcp.catalog import load_catalog, search

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_load_catalog_parses_pv_and_description(tmp_path):
    path = tmp_path / "cat.txt"
    path.write_text("usx:foo   a description here\n# a comment\n\nusx:bar   another\n")
    entries = load_catalog(path)
    assert len(entries) == 2
    assert entries[0].pv == "usx:foo"
    assert entries[0].description == "a description here"


def test_load_catalog_handles_missing_description(tmp_path):
    path = tmp_path / "cat.txt"
    path.write_text("usx:foo\n")
    entries = load_catalog(path)
    assert entries[0].pv == "usx:foo"
    assert entries[0].description == ""


def test_search_matches_pv_name_case_insensitively(tmp_path):
    path = tmp_path / "cat.txt"
    path.write_text("usxLAX:GSlit1V:t2.C   Guard slit vertical size\n")
    entries = load_catalog(path)
    results = search(entries, "gslit1v")
    assert len(results) == 1


def test_search_matches_description(tmp_path):
    path = tmp_path / "cat.txt"
    path.write_text("usxLAX:GSlit1V:t2.C   Guard slit vertical size\n")
    entries = load_catalog(path)
    results = search(entries, "guard slit")
    assert len(results) == 1


def test_search_empty_pattern_returns_nothing(tmp_path):
    path = tmp_path / "cat.txt"
    path.write_text("usx:foo   thing\n")
    entries = load_catalog(path)
    assert search(entries, "") == []
    assert search(entries, "   ") == []


def test_search_no_match_returns_empty_list(tmp_path):
    path = tmp_path / "cat.txt"
    path.write_text("usx:foo   thing\n")
    entries = load_catalog(path)
    assert search(entries, "nonexistent") == []


def test_real_usaxs_catalog_loads_and_is_searchable():
    entries = load_catalog(REPO_ROOT / "examples" / "pv_catalog_usaxs.txt")
    assert len(entries) > 20
    assert search(entries, "flux")
    assert search(entries, "ring current")
