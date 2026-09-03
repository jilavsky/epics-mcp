# Contributing to epics-mcp

## Before anything else

This package can, when configured to, change the state of a live scientific
instrument. Three rules that are not up for debate:

1. **The default test run must never open a Channel Access socket.** Tests
   use `epics_mcp.ca_client.FakeBackend`. Tests that genuinely need an IOC
   are marked `@pytest.mark.ioc` and are deselected by default; run them
   deliberately with `pytest -m ioc`.
2. **A change that widens what a policy permits needs a test that proves the
   old restriction still holds where it should.** `tests/test_examples.py`
   asserts properties of the shipped example policies directly — that the
   read-only policy grants zero writes, that no policy can reach `12idb*` or
   write a PSS record. Editing an example without updating that test is
   meant to fail CI.
3. **`src/epics_mcp/policy.py` imports nothing but the standard library and
   `yaml`.** No `mcp`, no `pyepics`. It is the piece other packages
   (aievaluator) can import, and it is the piece that has to be auditable by
   reading it.

## Setup

```bash
conda env create -f environment.yml
conda activate epics-mcp
pip install -e ".[dev]"
```

## Before opening a PR

```bash
ruff check .
pytest
```

`ruff` is the only linter and formatter — no black, no flake8. Settings live
in `pyproject.toml` (`line-length = 100`; `E741` stays off because `I`, `l`
and `Q` are physics notation).

## Conventions

- Python `>=3.10`; PEP 604 unions (`X | None`), `match`, `zip(strict=)` are
  all fair game.
- Google-style docstrings on anything public. English, in comments too.
- Prefer extending what exists over adding a parallel implementation. The
  one deliberate duplication in this package — `ca_client.PvReading` versus
  `aievaluator.epics_io.PvReading` — is argued for in `PLAN.md` §1.1; if you
  add a second one, argue for it in the same place.
- Keep EPICS record and field names exactly as beamline staff know them.
  Never rename a PV in a docstring to something "clearer".

## Changing a policy schema

The policy file is the safety boundary, so a schema change is a bigger deal
than it looks:

- Bump `version:` in the policy schema and keep the loader able to read the
  previous version, or fail with a message that says exactly what to edit.
- Update both shipped examples and `tests/test_examples.py`.
- Update `PLAN.md` §4 — it is the document a reviewer reads to decide
  whether the boundary still holds.
