# Tests

One file per `src/epics_mcp/` module, plus `test_examples.py` (properties of
`examples/*.yaml`) and `test_scaffolding.py` (bare import/packaging checks).
See `PLAN.md` 7 ("Testing strategy") for the rationale behind the split.

Two things to know before adding any:

- **Nothing in the default test run may open a Channel Access socket.** The
  fake CA backend (`epics_mcp.ca_client.FakeBackend`) is the only backend the
  default suite uses. Tests that genuinely need a live IOC -- or that touch
  a policy with an exact-name allow rule pointing at a real PV, since
  `doctor`'s `check_representative_pv` will try to connect to it -- are
  marked `@pytest.mark.ioc` and are deselected by `addopts` in
  `pyproject.toml`; run them at the beamline with `pytest -m ioc`.
- **The shipped example policies are test fixtures.** `tests/test_policy.py`
  and `tests/test_examples.py` assert properties of `examples/*.yaml`
  directly through the real `Policy` engine -- that
  `policy_usaxs_readonly.yaml` grants no writes under any mode, that neither
  policy can read or write a `12idb*`/`12idd*` PV, that no policy can write a
  PSS or storage-ring record, that every write rule carries a bound and no
  write rule targets a builtin-denied field. Editing an example policy
  without updating those tests is meant to fail CI.
