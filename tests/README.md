# Tests

No tests yet -- see `PLAN.md` 7 ("Testing strategy") for what goes here and
in what order.

Two things to know before writing any:

- **Nothing in the default test run may open a Channel Access socket.** The
  fake CA backend (`epics_mcp.ca_client.FakeBackend`) is the only backend the
  default suite uses. Tests that genuinely need a live IOC are marked
  `@pytest.mark.ioc` and are deselected by `addopts` in `pyproject.toml`; run
  them at the beamline with `pytest -m ioc`.
- **The shipped example policies are test fixtures.** `tests/test_examples.py`
  asserts properties of `examples/*.yaml` directly -- that
  `policy_usaxs_readonly.yaml` grants no writes, that neither policy can read
  a `12idb*` PV, that every write rule carries a bound. Editing an example
  policy without updating that test is meant to fail CI.
