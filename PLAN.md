# epics-mcp — implementation plan

**Status: proposal, 2026-09-03. No implementation code yet.**
Companion to `Aida/PLAN_INSTRUMENT_INTEGRATION.md` §4, which is the source
document for this package. That section decided *whether* to build it and
*where* it lives; this file decides *how*.

Read §1 for what this package is and is not, §4 for the safety model (the
part that actually matters), §6 for the order of work.

---

## 1. Scope

`epics-mcp` is a small, **generic**, **policy-gated** MCP server for EPICS
Channel Access. It gives an AI client three verbs — read a PV, describe a
PV, watch a PV for a bounded interval — and, only where a policy file
explicitly permits it, a fourth: write a PV within a stated bound.

**What it is not**, and must not become:

- **Not USAXS-specific.** Instrument judgment ("is the flux OK?", "does the
  undulator harmonic match the mono?") lives in `aievaluator`. This package
  does not know what a good value is; it knows what may be touched.
  If a tool here ever grows a threshold, it belongs in aievaluator instead.
- **Not a control system.** No scan orchestration, no motor moves as a
  first-class concept, no plan execution. Bluesky and `aievaluator` do that.
- **Not a discovery service.** `pv_search` searches a static text catalog,
  because Channel Access has no name lookup and pretending otherwise would
  invite a broadcast storm.

**Dependency arrow.** `aievaluator` may one day import `epics_mcp.policy` to
route its own `epics_io` reads through the same gate. `epics_mcp` must never
import `aievaluator` — that would drag Tiled, matplotlib and USAXS
thresholds into a package whose entire value proposition is that it is small
enough to audit.

### 1.1 Relationship to `aievaluator.epics_io`

`aievaluator/epics_io.py` already does careful, timeout-bounded, cached
pyepics reads and returns a well-shaped `PvReading` TypedDict. The house
rule is to extend what already does 80 % of the job, so this needs an
explicit answer: **`epics_mcp.ca_client` is a separate implementation, and
that is deliberate.** Importing `epics_io` would invert the dependency
arrow above. What we do instead:

- `epics_mcp.ca_client.PvReading` is **field-for-field identical** to
  `aievaluator.epics_io.PvReading` (`pv, value, units, connected, timestamp,
  severity, status, error`). A test asserts this by reflection when
  aievaluator happens to be importable, and skips when it is not.
- The duplicated surface is ~80 lines of get/wait/disconnect. The write
  path, the field introspection (`pv_info`), the monitor and the policy hook
  have no counterpart in `epics_io` at all.
- If the duplication ever bites, the merge direction is: aievaluator drops
  `epics_io` and depends on `epics-mcp` — not the reverse.

---

## 2. Deployment shape

One server process, one policy file, launched per client or as a service.

```
                        stdio (default)
  AIDA on usaxscontrol ────────────────► epics-mcp ──CA──► IOCs
                                          │ policy: usaxs-staff.yaml
                                          └─ audit-usaxs-staff.log

                        streamable-HTTP (phase 7)
  AIDA on a staff laptop ──────────────► epics-mcp on usaxscontrol
                            bearer token   (policy file is on the server host,
                                            so a laptop client cannot widen it)
```

**Switching instruments/configurations** (the requirement from the brief) is
"restart with a different `--policy`, and manual intervention is fine":

```bash
epics-mcp --policy ~/.epics-mcp/policies/usaxs-staff.yaml
epics-mcp --policy usaxs-staff     # shorthand: resolves in the policy dir
epics-mcp --policy saxs-user       # a different station, same binary
```

A bare name with no `/` and no `.yaml` resolves against
`$EPICS_MCP_POLICY_DIR` (default `~/.epics-mcp/policies/`). A path is used
as given. `EPICS_MCP_POLICY` supplies the default when the flag is absent.
There is **no default policy**: with neither flag nor env var the server
prints the resolution order and exits non-zero.

---

## 3. Tool surface

Six tools, all prefixed `epics_` so they stay unambiguous when AIDA has
`pyirena-mcp` and `aievaluator-mcp` connected at the same time — the same
convention `pyirena_*` already uses.

| Tool | Args | Returns |
|---|---|---|
| `epics_pv_get` | `names: list[str]` | one `PvReading` per name, in order |
| `epics_pv_info` | `name: str` | units, `LOPR`/`HOPR`, `DRVL`/`DRVH`, `PREC`, `.DESC`, enum strings, record type, writability under the current policy |
| `epics_pv_watch` | `names`, `seconds`, `interval` | bounded time series: `{pv: [[t, value], ...]}` |
| `epics_pv_put` | `name`, `value`, `confirm_token=None` | new value, old value, or a confirm challenge |
| `epics_policy_describe` | — | the effective policy in plain text |
| `epics_pv_search` | `pattern: str` | catalog entries matching, `{pv, description, readable, writable}` |

Registration is conditional: **`epics_pv_put` is not registered at all**
when the effective mode is read-only. A tool the client cannot see is a
stronger guarantee than a tool that refuses.

Two conventions inherited from AIDA's side (`PLAN_INSTRUMENT_INTEGRATION.md`
§2.2), and they are requirements, not preferences:

- **Nothing blocks and nothing raises for instrument conditions.** A
  disconnected PV is `{"connected": false, "error": "..."}`. A dead IOC must
  not turn a status question into a tool error, or the model will retry.
  Exceptions are reserved for policy denials and malformed arguments.
- **Structured content, not prose.** Return JSON; AIDA's `mcp/results.py`
  renders it. No Markdown formatting in this server.

### 3.1 `epics_policy_describe` earns its place

The model needs to know what it may do *before* it tries, or it wastes turns
guessing and reports "permission denied" as though the instrument were
broken. `policy_describe` returns the allow patterns with their notes, the
deny patterns, the mode, the write rules with their bounds, and the limits —
i.e. the policy file's own comments are part of the product. This is why
every example policy rule carries a `note:`.

---

## 4. The safety model

This is the section to argue with.

### 4.1 Layers, and which one is actually a boundary

1. **The policy file, enforced in this server process — the only real
   boundary.** Same file for every client of that server.
2. **AIDA's `mcp.json`** (`disabled_tools`, `confirm_tools`, `enabled_tools`)
   — a UI convenience layer. A different MCP client, or a headless
   `aida run`, bypasses it entirely. Useful, not a boundary.
3. **The workspace/group split** — `usaxs-user` never sees this server at
   all; `usaxs-staff` sees it with `confirm_tools: [epics_pv_put]`.

Layer 1 must be sufficient on its own. Design every question by asking: *if
layers 2 and 3 did not exist, would this still be safe?*

### 4.2 Policy resolution

```
                    ┌─ is there a policy? ──── no ──► refuse to start
                    │
  pv name ──────────┼─ built-in write denies (writes only) ──► DENY
                    ├─ deny: patterns ─────── match ────────► DENY
                    ├─ allow: patterns ────── no match ─────► DENY (default deny)
                    └─ writes: rules (puts) ─ no match ─────► DENY
                                             match ──► bound / enum / rate / token
```

Rules, stated so they can be tested:

- **No policy, no PVs.** The server refuses to start without a policy file.
  Not "starts with an empty policy" — refuses, loudly, with the resolution
  order printed.
- **Default deny.** An empty or absent `allow:` grants nothing.
- **Deny always wins**, over `allow` and over `writes`, for reads and writes
  alike. There is exactly one exception, §4.3.
- **Writes need everything**: `mode: read-write`, no `--readonly` override,
  no deny match, a `writes:` rule match, the value inside that rule's
  constraint, the rate limit, and — if the rule says so — a confirm token.
- **Mode is in the file, not in a tool argument.** Turning writes on is an
  edit to a file on disk by a human on the server host.
- **`--readonly` on the command line beats the file**, never the reverse.
  This is what lets a user-facing server and a staff server share one policy
  file if a site wants that.

### 4.3 The one asymmetry: broad deny + exact-name allow

"Readable but never writable" is a real need (the storage ring, the
undulator, the PSS beam-ready signals) and a single deny list cannot express
it, because deny covers reads too. The rule:

> A `deny:` pattern shadows an `allow:` pattern, **unless** the allow rule is
> an exact PV name — no `*`, no `?`, no `re:`.

So `deny: "PA:*"` plus `allow: "PA:12ID:STA_A_BEAMREADY_PL.VAL"` reads that
one signal and can never write it, and no future edit to `writes:` can reach
a PSS record. But `deny: "PA:*"` plus `allow: "PA:12ID:*"` is a **policy
load error**, not a silent widening — the loader rejects it at startup and
names both rules. The asymmetry is narrow on purpose: exact names are
auditable by eye, wildcards are not.

Alternative considered and rejected: separate `reads:`/`writes:` deny lists.
It expresses the same thing with more file, and it makes "this PV is
untouchable" require two edits instead of one — the failure mode being that
someone makes one of them.

### 4.4 Built-in write denies

Applied before the file, on writes only, whatever the patterns say:

```
*.PROC  *.STOP  *.SCAN  *.FLNK  *.INP  *.OUT  *.CALC  *.SDIS
*.DISA  *.DISV  *.FLNK  *.SIML  *.SIOL  *.TPRO  *.UDF
```

These change what a record *is* or how it is wired, and no legitimate agent
task needs them. Reads are unaffected (`caget foo.SCAN` is harmless and
diagnostically useful).

Escape hatch: `disable_builtin_write_denies: true` in the policy, which logs
a `WARNING` at startup and stamps `builtin_write_denies: disabled` into
every audit record. It exists because someone will eventually have a real
reason; making it loud and permanent in the audit trail is the price.

### 4.5 Write constraints

Per `writes:` rule, all optional, all AND-ed:

| Key | Meaning |
|---|---|
| `range: [lo, hi]` | numeric, inclusive; rejects NaN/inf; rejects non-numeric values |
| `enum: [...]` | value must be one of these (string or int; matched after enum-string resolution) |
| `step: n` | value must be a multiple of `n` away from the current value (guards fat-finger 10× errors) |
| `max_delta: n` | \|new − current\| ≤ n; requires a successful read first, and fails closed if the read fails |
| `units: "C"` | **documentation only**, echoed in `policy_describe` and the audit log — the server does not attempt unit conversion, ever |
| `confirm: true` | requires a confirm token, §4.6 |
| `rate_limit_per_min: n` | token bucket per rule |

Plus a policy-wide `max_writes_per_min`, so a runaway agent hits a ceiling
even across rules.

`units` being documentation only is worth stating loudly in the docs: a
policy that says `units: "C"` on a PV whose record is in kelvin will happily
write 300 K as though it were 300 °C. Bounds protect you; the label does not.

### 4.6 Confirm tokens

For rules with `confirm: true`, the first `epics_pv_put` never writes:

```json
{"status": "confirm_required",
 "token": "b1f4…", "expires_in_s": 60,
 "pv": "usxTEMP:linkam:SP", "current_value": 25.0, "requested_value": 250.0,
 "rule": "usxTEMP:*:SP  range [20.0, 300.0]  units C"}
```

The client resends with `confirm_token`. The token is single-use, bound to
`(pv, exact value, session)`, and expires in 60 s.

This is not theatre. It does two useful things: it forces the model to state
the same intent twice with the current value in front of it (a genuine check
against a hallucinated PV name or a units slip), and it puts a *rendered*
tool call with old and new values in the human's transcript before anything
moves.

### 4.7 Audit log

Append-only JSON Lines, one object per gated operation. Written before the
CA call completes for writes (intent), then a second record with the
outcome — so a write that hangs still leaves a trace.

```json
{"ts":"2026-09-03T18:22:41.113Z","event":"put","phase":"intent",
 "pv":"usxLAX:userCalc1.A","old":1.0,"new":2.5,
 "rule":"usxLAX:userCalc*.A","client":"aida/0.1.0b3","session":"3f9c",
 "policy":"usaxs-staff.yaml","policy_sha256":"9ab3…","mode":"read-write"}
```

- `log_reads` and `log_denials` are policy switches; denials default on
  (a stream of denials is how you find out the policy is wrong, or that
  something is probing).
- Size-based rotation, `audit.max_bytes` / `audit.keep`, no external dep.
- The `policy_sha256` field is the point of the whole record: it says which
  policy text was in force, so "who changed the policy and when" is
  answerable from the log alone.
- The server is the only component that reliably knows timestamp, PV, old
  value, new value and client identity together. That is why the log lives
  here and not in AIDA.

### 4.8 What we are choosing not to defend against

Stated so nobody assumes otherwise:

- **A hostile operator of the server host.** Anyone who can edit the policy
  file can grant anything. This is a guard rail against an agent's mistakes
  and an operator's typos, not against an attacker with shell access.
- **Channel Access itself.** CA has no authentication. Anything that can
  reach the subnet can `caput` regardless of this server. The policy narrows
  what *this* path can do; it does not secure the instrument.
- **Array and waveform writes.** Not supported in v1 at all. Scalars only.

---

## 5. Modules

```
src/epics_mcp/
├── __init__.py        version only
├── policy.py          load + validate YAML; Decision = match(name, op)
├── ca_client.py       CaBackend protocol; PyepicsBackend, FakeBackend
├── catalog.py         parse pv_catalog.txt; search
├── audit.py           append-only JSON Lines, rotation
├── ratelimit.py       token buckets, confirm-token store
├── server.py          FastMCP tools; stdio + streamable-HTTP
├── doctor.py          self-check: policy valid? CA up? audit writable?
└── cli.py             argument parsing, policy resolution, transport choice
```

Two properties this layout is chosen for:

- **`policy.py` imports nothing but stdlib and yaml.** No `mcp`, no
  `pyepics`. It is pure, it is the piece with lasting value even if the MCP
  server were never used, and aievaluator can import it as-is.
- **`ca_client.py` is the only module that imports `epics`**, lazily, on
  first use — the same pattern `aievaluator/epics_io.py` uses, and for the
  same reason: `--help`, `--check`, policy validation and the entire default
  test suite must work on a laptop with no EPICS installed.

### 5.1 `policy.py` shape

```python
@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str            # "allow: usx*" | "deny: re:^12idb.*" | "default deny"
    rule: WriteRule | None # the matched write rule, for constraint checking

class Policy:
    @classmethod
    def load(cls, path: Path, *, force_readonly: bool = False) -> "Policy": ...
    def can_read(self, pv: str) -> Decision: ...
    def can_write(self, pv: str, value) -> Decision: ...
    def describe(self) -> dict: ...
    sha256: str
```

Pattern matching: `fnmatch.fnmatchcase` by default (case-sensitive — EPICS
PV names are), `re:` prefix for `re.fullmatch`. A malformed regex is a load
error, not a runtime surprise. Compiled once at load.

### 5.2 `ca_client.py` shape

```python
class CaBackend(Protocol):
    def get(self, pv: str, timeout: float) -> PvReading: ...
    def get_many(self, pvs: list[str], timeout: float) -> list[PvReading]: ...
    def info(self, pv: str, timeout: float) -> dict: ...
    def put(self, pv: str, value, timeout: float) -> PvReading: ...
    def monitor(self, pvs, seconds, interval) -> dict[str, list]: ...
```

`PyepicsBackend` (default) and `FakeBackend` (`--backend fake`, reads canned
values from a YAML — used by the tests and by anyone demoing the server off
the beamline). `caproto` stays a future one-module swap, per
`PLAN_INSTRUMENT_INTEGRATION.md` §4.2; PVAccess (`p4p`) only if a concrete
need appears.

`monitor` is polled, not `camonitor`-subscribed, in v1: bounded, trivially
cancellable, and `interval` is a floor not a guarantee. Subscription-based
monitoring is a later optimisation if the poll rate is ever a problem.

---

## 6. Phases

Each phase is useful on its own and none is undone by the next. Phase 0 is
this commit.

| # | Deliverable | Done when | Status |
|---|---|---|---|
| **0** | Repo, packaging, environment, this plan, example policies | *(this commit)* | done |
| **1** | `policy.py` + its test matrix | The shipped example policies load, and the deny/allow/exact-name-exception table is green | done |
| **2** | `ca_client.py`: `PvReading`, `FakeBackend`, `PyepicsBackend` | `pytest` green with no EPICS installed | done -- built get/get_many/info/put/monitor together rather than splitting put across phases 2 and 6, since the interface is one small `Protocol` either way; nothing exposes `put` until phase 6's tool gate says so |
| **3** | `catalog.py`, `audit.py`, `ratelimit.py` | Rotation and token expiry tested with a frozen clock | done |
| **4** | `server.py` read tools + `cli.py` stdio: `pv_get`, `pv_info`, `pv_search`, `policy_describe` | A real MCP client connects over stdio and answers "what is `usxLAX:m58:c0:m1.RBV`?" against `FakeBackend` | done -- verified against a real `ClientSession` over stdio, not just in-process |
| **5** | `pv_watch` | Bounded, cancellable, capped by `max_watch_seconds`/`max_watch_samples` | done |
| **6** | `pv_put`, confirm tokens, constraints, write audit | Write path exercised end-to-end against `FakeBackend`; then against a local `softIoc` under `-m ioc` | code done and exercised end-to-end against `FakeBackend` (deny/range/confirm-token/rate-limit/audit all verified over a real stdio session); **not yet exercised against a `softIoc` or any live IOC**, and not yet run read-only at a real beamline for any length of time -- see the gate below, which still applies to *deployment*, just not to writing the code |
| **7** | `--transport http`, `doctor`, wheel smoke test in CI | `epics-mcp doctor --policy ...` on usaxscontrol reports green | partial -- `doctor` done (mirrors `aievaluator/doctor.py`); `--transport http` serves via `streamable-http` with host/port but **no bearer-token auth yet**, and confirm-token/audit identity is not yet bound to a specific client connection (fine for stdio's one-process-per-client shape, not fine once one HTTP server serves several laptops at once -- PLAN.md 3's "session identity for v1" note); wheel smoke test in CI extended to run the console script and check the no-policy exit code |
| **8** | AIDA integration: `aida mcp add-epics` preset, `usaxs-staff` workspace, a skill | Staff can ask the question in AIDA without editing `mcp.json` by hand | not started |

**The phase 6 gate is about deployment, not about writing the code.** Phase
6's write path is implemented and tested against `FakeBackend` -- that is
what "test to see how this works" (2026-09-03) asked for, and building the
machinery (confirm tokens, rate limits, the audit intent/outcome pairing)
alongside the read tools avoided a later re-architecture. What has *not*
happened, and must not be skipped before any real deployment: running
phases 1-5 read-only at the beamline long enough for the audit log to show
what the model actually asks for, and exercising the write path against a
real IOC (`softIoc` first, then the real instrument) under `-m ioc`. Do not
point `epics-mcp --policy usaxs-staff` (or any read-write policy) at real
Channel Access without doing both first.

---

## 7. Testing strategy

The policy engine is pure and must be tested exhaustively; Channel Access is
faked; a live IOC is opt-in.

- **`tests/test_policy.py`** — a table of `(policy, pv, op, expected)` rows.
  Every rule in §4.2 gets rows, including the ones that must *fail to load*
  (deny shadowing a wildcard allow, malformed regex, `range` on a non-numeric
  rule, `mode: read-write` with an empty `writes:`).
- **`tests/test_examples.py`** — properties of the shipped `examples/*.yaml`,
  asserted directly:
  - `policy_usaxs_readonly.yaml` grants **zero** writes, under any mode.
  - Neither policy can read or write any `12idb*` or `12idd*` PV.
  - No policy can write a PSS (`PA:*`) or storage-ring (`XFD:*`) PV.
  - Every rule in `writes:` has at least one of `range` / `enum`.
  - Every PV in `pv_catalog_usaxs.txt` parses and is either allowed or
    explicitly denied — no silent "not in any list" entries.
  Editing an example policy without updating this test is meant to fail CI.
- **`tests/test_ca_client.py`** — `FakeBackend` covers good / bad /
  disconnected / timeout / wrong-type branches. A reflection test asserts
  `PvReading` still matches `aievaluator.epics_io.PvReading`, skipped when
  aievaluator is not importable.
- **`tests/test_server.py`** — the FastMCP tools against `FakeBackend`
  in-process: tool registration (is `epics_pv_put` absent in read-only?),
  argument validation, `max_pvs_per_call`, the confirm-token round trip.
- **`tests/test_audit.py`** — record shape, rotation, `policy_sha256`
  stability, intent-then-outcome pairing on a write that raises.
- **`@pytest.mark.ioc`** — deselected by default (`addopts` in
  `pyproject.toml`). Run at the beamline, or against `softIoc` with a small
  `.db` in `tests/ioc/`, with `pytest -m ioc`.

Non-negotiable: **nothing in the default test run opens a CA socket.** A test
suite that broadcasts on the beamline network when someone runs `pytest` on
usaxscontrol is a bug in the test suite.

---

## 8. Open questions

1. **Which conda env hosts this on `usaxscontrol`** — its own, or the shared
   pyirena/aievaluator/AIDA one? (`PLAN_INSTRUMENT_INTEGRATION.md` §5 asks
   this too.) The shared env is one fewer interpreter path in AIDA's
   `mcp.json`; a separate env means a pyepics upgrade for this server cannot
   disturb aievaluator. Leaning shared, because they are the same pyepics.
2. **Who owns the deployed policy file**, and does it live in `~/.epics-mcp/`
   (per-user, easy) or `/etc/epics-mcp/` root-owned (a real boundary against
   an agent that has been given a file-write tool)? The second is
   meaningfully safer the moment AIDA's coding tools are enabled in the same
   workspace, and costs one `sudo` per policy change.
3. **Does `pv_info` need `.DESC` on every read?** It is one extra CA call per
   PV. Proposal: no — `pv_get` stays lean, `pv_info` is the tool that pays
   for metadata.
4. **The `usxTEMP:*:SP` write rule in `policy_usaxs_staff.yaml` is
   unverified** — the prefix came from the design document, not from a
   `caget`. Someone has to confirm the real Linkam/sample-environment
   setpoint record and its units before that file is deployed anywhere. Same
   for `usxRIO:GalilAo1_SP.VAL`, which is left commented out.
5. **Should there be an `epics_pv_put` dry-run mode** (`dry_run: true`
   returning what would happen without writing)? It would let the model
   check a value against the policy without spending a confirm token. Cheap;
   deferred until phase 6 shows whether it is needed.
