# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Repository scaffolding: `pyproject.toml` (setuptools, src layout),
  `environment.yml` (conda, co-installable with pyirena/aievaluator/AIDA),
  `.gitignore`, MIT `LICENSE`, CI workflow, `CONTRIBUTING.md`.
- `PLAN.md` — the design under review: scope, deployment shape, tool
  surface, safety model, module layout, phased implementation order,
  testing strategy, open questions.
- Example policies: `examples/policy_usaxs_readonly.yaml` (no writes) and
  `examples/policy_usaxs_staff.yaml` (bounded writes, `mode: read-write`),
  plus `examples/pv_catalog_usaxs.txt` for `epics_pv_search`. PV names are
  drawn from `aievaluator/config/instrument_usaxs.yaml`; the write rules
  marked `TODO(verify)` have not been checked against a live IOC.

### Not yet implemented
- Everything under `src/epics_mcp/` except `__version__`. See `PLAN.md` §6
  for the phase order. Nothing in this repository can reach an instrument.
