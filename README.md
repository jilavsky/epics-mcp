# epics-mcp

A small, generic, **policy-gated** MCP server for EPICS Channel Access.

It lets an AI assistant answer "what is `usxLAX:m58:c0:m1.RBV` right now?"
or "has the Linkam reached temperature?" — and, only where a policy file
explicitly permits it and within bounds that file states, change a value.

> **Status: alpha.** The read path, the policy engine, the audit log and the
> constrained write path are implemented and tested, and the read tools have
> been exercised against a live USAXS instrument through a CA gateway.
>
> **The write path has never touched a real IOC** — only the fake backend —
> and has not run read-only at a beamline long enough to learn what an agent
> actually asks for. Do not point a `mode: read-write` policy at real
> Channel Access until both have happened (see [`PLAN.md`](PLAN.md) §6).
> The HTTP transport works but has no bearer-token auth yet.

## Why a separate package

The design rationale lives in
`Aida/PLAN_INSTRUMENT_INTEGRATION.md` §4. In short: instrument *judgment*
("is the flux OK?") belongs in [`aievaluator`](https://github.com/jilavsky/aievaluator),
which is USAXS-specific and read-only by construction. Generic, potentially
write-capable channel access is a different product with a different safety
profile, and keeping it in its own package keeps `aievaluator-mcp` trivially
auditable.

## The safety model in one paragraph

A YAML policy file, given on the command line, is the hard boundary — it is
enforced inside this server process, so a different MCP client or a headless
run cannot bypass it. **No policy file, no PVs**: the server refuses to
start without one, and an empty allow-list grants nothing. Denies always beat
allows. Writes need all of: `mode: read-write` in the file, no `--readonly`
on the command line, a matching rule under `writes:`, a value inside that
rule's declared bound, a rate-limit slot, and (for rules that ask for it) a
single-use confirm token. Every gated operation lands in an append-only
audit log stamped with the SHA-256 of the policy text that permitted it.
Client-side controls — AIDA's `confirm_tools`, `disabled_tools` — are a
useful second layer, not a boundary. See [`PLAN.md`](PLAN.md) §4.

## Tool surface

| Tool | Purpose |
|---|---|
| `epics_pv_get` | values for up to `max_pvs_per_call` PVs |
| `epics_pv_info` | units, limits, precision, enum strings, `.DESC`, field type, element count |
| `epics_pv_watch` | bounded short monitor — "is it still moving?" |
| `epics_pv_put` | constrained write; **not registered at all** in read-only mode |
| `epics_policy_describe` | the effective policy in plain text |
| `epics_pv_search` | search a static PV catalog file (CA has no name lookup) |

No `pv_list_all`, no array writes, no `.PROC`, no unbounded monitors.

### What a reading looks like

Values are coerced into JSON-safe shapes before they reach the client, which
matters more than it sounds: EPICS stores any string longer than 40
characters as a CHAR waveform, so a naive read of `usxLAX:userDir` returns an
array of 39 integers rather than
`/share1/USAXS_data/2026-09/09_12_Randy`.

- **CHAR waveforms decode to strings** (paths, sample titles, status text).
- **Numeric waveforms come back as lists**, truncated to `max_array_points`
  (default 100). The reading's `count` is always the *true* element count and
  `truncated` says whether you are seeing all of it — an 8000-point scan
  array arrives as 100 numbers with `count: 8000, truncated: true`.
- **Enum records carry their label**: `value: 0, enum_string: "Passive"`.
- **`NaN`/`Infinity` become `null`**, because they are not valid JSON and one
  undefined reading would otherwise make a whole response unparseable.

See [`PLAN.md`](PLAN.md) §3.2 for the full table and the reasoning.

## Install

Into its own environment:

```bash
conda env create -f environment.yml
conda activate epics-mcp
```

Or into the environment that already holds pyirena / aievaluator / AIDA —
the usual choice on a control machine, since AIDA launches this server as a
subprocess and one env means one interpreter path to configure:

```bash
conda activate pyirena
conda install -c conda-forge pyepics
pip install -e .
```

## Configure

Copy an example policy; never edit the shipped one in place.

```bash
mkdir -p ~/.epics-mcp/policies
cp examples/policy_usaxs_readonly.yaml ~/.epics-mcp/policies/usaxs-user.yaml
$EDITOR ~/.epics-mcp/policies/usaxs-user.yaml
```

- [`examples/policy_usaxs_readonly.yaml`](examples/policy_usaxs_readonly.yaml)
  — reads only, nothing writable. Start here.
- [`examples/policy_usaxs_staff.yaml`](examples/policy_usaxs_staff.yaml)
  — the same reads plus a small, bounded, rate-limited set of writable PVs.
  **Read its header before deploying it**; some of its rules name PVs taken
  from a design document rather than from a live `caget`.
- [`examples/pv_catalog_usaxs.txt`](examples/pv_catalog_usaxs.txt)
  — human-readable names for `epics_pv_search`. Documentation, not a
  boundary: listing a PV here does not grant access to it.

## Run

```bash
epics-mcp --policy usaxs-user                   # resolves in ~/.epics-mcp/policies/
epics-mcp --policy /etc/epics-mcp/usaxs.yaml    # or an explicit path
epics-mcp --policy usaxs-staff --readonly       # force read-only, whatever the file says
epics-mcp --policy usaxs-staff --check          # validate, print effective rules, exit
epics-mcp doctor --policy usaxs-staff           # CA reachable? catalog present? audit writable?
```

Switching between instruments or configurations is a restart with a
different `--policy`; several policies can live side by side in the policy
directory.

For a staff laptop that cannot do Channel Access itself, run the server on
the control machine and connect over HTTP (planned, phase 7):

```bash
epics-mcp --policy usaxs-staff --transport http --host 0.0.0.0 --port 8765
```

The policy file lives on the server host, so a remote client cannot widen it.

## Related

- [AIDA](https://github.com/jilavsky/aida) — the agent workbench that
  launches this server.
- [aievaluator](https://github.com/jilavsky/aievaluator) — USAXS instrument
  fitness checks. May one day import `epics_mcp.policy`; this package must
  never import it.
- [pyirena](https://github.com/jilavsky/pyirena) — SAS analysis, and the
  `pyirena-mcp` server this one is modelled on.

## License

MIT — see [LICENSE](LICENSE).
