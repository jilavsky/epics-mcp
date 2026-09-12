# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **String and waveform PVs are now readable.** `epics_pv_get` failed
  outright on any array-valued PV with
  `Unable to serialize unknown type: <class 'numpy.ndarray'>`, because raw
  pyepics values went into the MCP response unconverted. Reported against
  `usxLAX:userDir`, which EPICS stores as a 1022-element CHAR waveform — the
  way it stores any string over 40 characters. All values are now coerced in
  `ca_client.coerce_ca_value` (PLAN.md §3.2):
  - CHAR waveforms decode to strings, NUL-terminated, signed bytes handled.
  - numpy scalars and arrays become Python scalars and lists; arrays are
    truncated to the new `max_array_points` policy setting (default 100),
    with the reading's `count` always reporting the true element count and
    `truncated` saying whether anything was dropped.
  - enum records gain `enum_string`, their label (`value: 0,
    enum_string: "Passive"`), since the bare index tells the model nothing.
  - `NaN`/`±Inf` become `null`. `json.dumps` renders them as the bare
    literals `NaN`/`Infinity`, which are not valid JSON, so a single
    undefined reading could make an entire tool response unparseable.
  - `bytes` decode to strings.
  Verified against the live USAXS instrument over a CA gateway for CHAR
  waveforms, double/float waveforms (8000-point sscan arrays), enums,
  `DBF_STRING` and double scalars.
- `epics_pv_info`'s `record_type` was never the record type — it held
  pyepics' CA *field* type. Renamed to `field_type` (the record type lives in
  `.RTYP` and would cost an extra round trip), and `count` added alongside so
  the model can tell in advance whether a read will be truncated.
- A connected PV whose `get` returned nothing now reports a reading with an
  error rather than a `None` value.
- `pv_info`'s display/control limits are passed through the same non-finite
  guard as values.

### Changed
- `PvReading` is now a documented *superset* of
  `aievaluator.epics_io.PvReading` rather than field-for-field identical,
  adding `count`, `truncated` and `enum_string`. The reflection test was
  split in two: one asserts no aievaluator field went missing (the half that
  protects interoperability), one pins the extras to exactly those three.
  See PLAN.md §1.1.
- `PLAN.md` and `README.md` status lines updated to match what is actually
  built; PLAN.md gains §3.2 documenting the value-coercion contract and the
  three judgment calls in it (truncate rather than decimate; a CHAR array is
  always a string; `count` never lies).

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
