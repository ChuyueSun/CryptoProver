# Website backend: driving the decompression demo

How a website shells out to this harness to run the Edwards **point
decompression** proof-synthesis demo. The integration boundary is a single
script — `demo_decompress.sh` — that owns all machine-specific setup. The
website launches it and then **polls files under `results/<run-id>/`**.

## The backend in one sentence

`run.py` is the entire backend: one target per invocation, it spawns
`claude -p` in a round loop, runs `cargo verus` after each round, and writes
**everything as JSON under `results/`**. There is no server and no HTTP API —
you run a process and read files. Exit codes: `0`=launched/ok, `1`=proof
failed, `42`=rate-limited.

> **`demo_decompress.sh` remains the website's single-command boundary** — it
> owns the machine-specific setup and the reset-in-place worktree the demo polls,
> so the website integration is unchanged. For non-website experiment runs the
> build-side has been reconciled into **peel** (`peel.py` + `peel_manifests/` +
> `peel_run.sh`): the same proof-stack cuts, expressed as data on one
> peel-depth axis. The `demo_decompress.sh` flags are run-side labels; the two
> front-page modes below map to
> `peel_manifests/decompress_proof_only.json` (`proof-only`) and the gate-OFF
> `spec-proof` cut. See `docs/spec_gen_runbook.md` §1 and `peel_manifests/README.md`.

## Front-page modes

| Mode | Backend | What the agent rebuilds |
|------|---------|-------------------------|
| **Toy** | self-contained (no `run.py`) | unchanged |
| **dalek · formal spec** | `run.py --experiment-mode proof-only` (gate **ON**) | **proofs** only |
| **dalek · no spec** | `run.py --experiment-mode spec-proof` (gate **OFF**) | **contract + proofs** |

The front-page dalek modes are decompression via `run.py`, editing exactly
**one** dependency file, `decompress_lemmas.rs`. The anchor (the fixed contract
under test) is `edwards.rs::decompress` and is never edited in those front-page
modes. Deeper research flags such as `--no-anchor-proof`, `--no-bridge-*`, and
the Ristretto/full-stack flags may edit additional files or strip anchor proof
bodies; they are listed below as proof-stack cuts, not front-page modes.

> **Mode mapping is the easy thing to get backwards:**
> *formal spec → proof-only* (specs given, prove them), *no spec → spec-proof*
> (specs stripped, infer + prove). Not the other way around.

### Proof-stack cuts — exactly what `demo_decompress.sh` strips

`demo_decompress.sh` has nine mode labels overall. The table below lists the
seven flag-driven decompression/bridge cuts in increasing difficulty:
flag → what survives in the editable file(s) → what the agent must produce.

| Flag | doc `///` | signature | formal contract | proof body | agent produces |
|------|:--:|:--:|:--:|:--:|----------------|
| `--formal-spec` | kept | kept | kept | `admit()` | proofs |
| `--no-spec` | kept | kept | **stripped** | `admit()` | contracts + proofs |
| `--no-spec --strip-docs` | **stripped** | kept | **stripped** | `admit()` | contracts + proofs (no NL hints) |
| `--no-lemmas` | **deleted** | **deleted** | **deleted** | **deleted** | invent the lemmas from the `edwards.rs` callsites |
| `--no-anchor-proof` | **deleted** | **deleted** | **deleted** | **deleted** + **anchor's own proof body stripped** | reconstruct decompress's orchestration proof **and** invent the lemmas — with the anchor contract frozen |
| `--no-bridge-specs` | n/a | **deleted** | n/a | n/a | reconstruct the two Montgomery↔Edwards `open spec fn` **map definitions** so the whole crate verifies (the bridge module is editable; everything else frozen) |
| `--no-bridge-lemmas` | n/a | **all spec defs frozen** (incl. the map) | API contracts **frozen** | all 10 decompress-path lemmas **deleted** (sig + contract + body) | **proof reconstruction only**: re-derive all 10 deleted lemmas (5 decompress + 5 curve, incl. `x_zero_implies_y_squared_one`, `unique_x_with_parity`) — signature + contract + proof each — across 2 editable lemma files. No spec definition is reconstructed, and a re-derived lemma contract can't weaken the frozen API contracts, so the user-facing guarantee is structurally un-weakenable |

Notes:
- The `///` doc comments carry the *informal* proof sketch (case splits, the
  "odd − even = odd" reasoning). Plain `--no-spec` leaves them, so it is not a
  truly spec-free task — use `--strip-docs` for that.
- The signature (name `lemma_sign_bit_after_conditional_negate` + typed params)
  is itself a strong hint; only `--no-lemmas`/`--no-anchor-proof` remove it.
- `--no-lemmas` deletes every `proof fn` (discovered dynamically) but keeps
  imports + the `verus!{}` wrapper, so the file still parses; `edwards.rs` then
  fails with "cannot find lemma…", which is the agent's starting signal. The
  module-level `//!` doc is *not* stripped (a minor intent leak).
- `--no-anchor-proof` is the only rung that edits the **anchor file**: it strips
  `decompress`'s own `proof { … }` blocks (keeping signature + requires/ensures
  byte-identical to `main`) and deletes only the decompress-*only* helper
  lemmas. Because the leaked lemma names are gone from `edwards.rs`, the round-1
  signal flips from "cannot find lemma…" to **"postcondition not satisfied"** on
  `decompress` — see its own section below.
- `--formal-spec` → `proof-only` (gate ON); `--no-spec` and `--no-lemmas` →
  `spec-proof --no-spec-gate` (the agent rewrites/creates contracts, so the
  drift gate must be off); `--no-anchor-proof` → `contract-only` (gate **ON** —
  the frozen contract is what keeps it sound while the body is rebuilt);
  `--no-bridge-specs` → `bridge-specs` and `--no-bridge-lemmas` → `bridge-full`
  (both gate **ON** + whole-crate verify + frozen-file guard; the bridge rungs
  edit shared spec/lemma vocabulary that frozen consumers pin).
- `--no-bridge-lemmas` reconstructs the **whole decompress proof tree, cut below
  the curve layer** (field arithmetic + `vstd` stay as assumed primitives), as a
  **pure proof-reconstruction** task: *every* spec definition is frozen — the
  vocabulary predicates (`is_well_formed_edwards_point`, …) **and the map**
  (`montgomery_to_edwards_affine`, `edwards_y_from_montgomery_u`). The agent never
  reconstructs a definition, so the user-facing API contract is **structurally
  un-weakenable** (you can't weaken the meaning of something you can't write).
  Two files are editable: `decompress_lemmas.rs`, `curve_equation_lemmas.rs`
  (the map module `decompress_bridge_specs.rs` is frozen). **Every decompress-path
  lemma is deleted outright** (signature + contract + body); the agent re-derives
  each one. The 10:
  - `decompress_lemmas.rs`: `lemma_decompress_valid_branch`,
    `lemma_to_edwards_correctness`, `lemma_decompress_field_element_sign_bit`,
    `lemma_decompress_spec_matches_point`, `lemma_sign_bit_after_conditional_negate`;
  - `curve_equation_lemmas.rs`: `lemma_negation_preserves_curve`,
    `lemma_affine_to_extended_valid`, `lemma_edwards_affine_when_z_is_one`,
    `lemma_x_zero_implies_y_squared_one`, `lemma_unique_x_with_parity`.

  Deleting (rather than keeping their contracts) is contract-safe: the API
  ensures live in frozen files and reference only frozen spec fns — and the two
  editable files contain **zero** spec fns — so no re-derived lemma contract can
  weaken the user-facing guarantee (a too-weak one just fails the frozen proof →
  not COMPLETE). The list is a fixed property of the pinned source, so
  `demo_decompress.sh` names it explicitly (deterministic, no scan). The ~47 unrelated
  group-law lemmas (addition, scalar mult, niels) are left intact; the
  spec-integrity gate snapshots `decompress_lemmas.rs` + `curve_equation_lemmas.rs`
  (both `edwards.rs` siblings) **after** the strip, so every surviving contract is
  frozen while the agent may still *add* new helper lemmas (verify tolerates
  additions).

## The integration boundary: `demo_decompress.sh`

```
./demo_decompress.sh --formal-spec|--no-spec [--strip-docs]|--no-lemmas \
    --run-id <id> [--rounds N] [--budget MIN] [--model opus]
```

It owns, internally, everything the website must NOT re-derive:

1. **Env prelude** — puts uv-Python 3.14 (ahead of Apple's 3.9, which silently
   breaks the skills) and the prebuilt verus (`cargo verus`) on `PATH`; the
   worktree pins rustc 1.92.0 via its `rust-toolchain.toml`.
2. **Auth** — uses `CLAUDE_CODE_OAUTH_TOKEN` if set, else reads it from
   `DALEK_DEMO_TOKEN_FILE`, else falls back to the keychain login.
3. **One-time build warm** — a cold module-scoped verus check spuriously
   fails; the script warms once (sentinel `target/.demo_warmed`).
4. **Input prep** (canonical + idempotent) — resets `decompress_lemmas.rs` to
   clean `main`, then:
   - `--formal-spec`: `admit.py --mode fn-bodies` (admit bodies, keep contract)
   - `--no-spec`: `strip_specs.py` (strip contract) **+** `admit.py --mode fn-bodies`
5. **Detached launch** — re-execs `run.py` via `start_new_session` so the run
   survives the caller's process-group teardown (an attached run gets killed).

### Contract with the caller

- Returns **immediately** after launching; prints machine-readable lines:
  ```
  RUN_ID   web_1718655000
  RESULTS  /path/to/cryptoprover/results/web_1718655000/edwards
  MODE     no-spec
  LOG      /path/to/cryptoprover/launcher_web_1718655000.log
  PID      12345
  ```
- Exits **0** once launched; exits **nonzero only on launch/setup failure**.
  The *proof outcome* is read by the caller from `result.json` (see below).

### Env overrides (verified defaults baked in)

`DALEK_UV_PY_BIN`, `DALEK_VERUS_DIR`, `DALEK_PROJECT`, `DALEK_GITROOT`,
`DALEK_VSTD`, and either `CLAUDE_CODE_OAUTH_TOKEN` or `DALEK_DEMO_TOKEN_FILE`.

## Polling `results/<run-id>/edwards/`

Everything the UI renders is on disk:

| File | Render as |
|------|-----------|
| `claude_raw/round_N.jsonl` | **live agent activity** — stream the events (thinking + tool calls) |
| `round_N.json` | per-round **proof status**: `verus_okay` (bool) + `verus_errors[]` (`{file,line,severity,data}`) |
| `snapshots/round_N/decompress_lemmas.rs` | the **reconstructed file** each round — diff vs `snapshots/round_0/…` to show what was synthesized |
| `prompt_rendered.md` | the exact task the agent received |
| `result.json` | **terminal state**: `end_reason` (`COMPLETE`/`LIMIT`/`SPEC_DRIFT`/`RATE_LIMITED`/…), `success` (bool), `rounds_used`, `duration_seconds` |

Suggested website flow (`specProof.mjs`):

```
demo_decompress.sh --<mode> --run-id web_<ts>
  → parse RUN_ID / RESULTS from stdout
  → poll RESULTS/round_N.jsonl       (stream agent activity)
  → poll RESULTS/round_N.json        (verus_okay / verus_errors panel)
  → on each new round, diff RESULTS/snapshots/round_N/decompress_lemmas.rs
  → when RESULTS/result.json appears: show end_reason / success
```

A `COMPLETE` means `cargo verus` passed **and** zero `admit()` remain across
the target + the edited dep (the gate counts both — a lemma left as `admit()`
cannot pass even if `edwards.rs` verifies).

## What "done & genuine" looks like

Reference run already on disk: `results/decompress_specproof_002/` —
*no-spec* / spec-proof, Opus, `COMPLETE` in 1 round. Its
`snapshots/round_1/decompress_lemmas.rs` has `admit()=0` and all 13 spec
clauses reconstructed, zero `admit`/`assume`/`#[verifier::external_body]`.

## Wiring the `--no-lemmas` button (the showpiece)

This is the hardest, most impressive rung — the dep starts with **every proof
lemma deleted**, and the agent invents them from the `edwards.rs` callsites.

**Launch** (identical contract to the other modes — only the flag changes):
```bash
DALEK_DEMO_TOKEN_FILE=/path/to/token \
  ./demo_decompress.sh --no-lemmas --run-id web_<ts> [--rounds 5] [--budget 60]
# stdout → RUN_ID / RESULTS / MODE / LOG / PID ; exit 0 = launched
```
- Auth: set `CLAUDE_CODE_OAUTH_TOKEN` or point `DALEK_DEMO_TOKEN_FILE` at a file
  holding the `<claude-oauth-token>` value (never inline it).
- Give it more headroom than the easier rungs: `--rounds 5 --budget 60`.

**Starting state the user sees** (great for the "before" pane): open
`RESULTS/snapshots/round_0/decompress_lemmas.rs` — it's just imports + the
`verus!{}` wrapper, **0 `proof fn`**. The very first `round_1.json` shows
`verus_okay:false` with errors like *"cannot find function
`lemma_decompress_valid_branch`"* at the `edwards.rs::decompress` callsite —
that's the agent's starting signal.

**Poll loop** is exactly the same as every other listed mode (`round_N.jsonl` →
activity, `round_N.json` → verus status, `snapshots/round_N/…` → diff,
`result.json` → terminal). For the no-lemmas cut, the single-file diff is the
most dramatic: the file goes from ~empty to **5 fully reconstructed lemmas**.

**Reference run proving it converges:** `results/nolemmas_ref_001/` — Opus,
`COMPLETE` in **1 round / 163s**. `snapshots/round_1/decompress_lemmas.rs`
contains all five lemmas invented from scratch
(`lemma_sign_bit_after_conditional_negate`,
`lemma_decompress_field_element_sign_bit`, `lemma_decompress_valid_branch`,
`lemma_decompress_spec_matches_point`, `lemma_to_edwards_correctness`), 13 spec
clauses, **zero `admit`/`assume`/`external_body`**. Use it as the canned
"solved" example or a golden test.

**Don't run it concurrently with another demo on the same worktree** (see
Caveats) — until the pool lands, serialize `--no-lemmas` against any other
`demo_decompress.sh` call.

## Wiring the `--no-anchor-proof` button (the hardest rung)

The 5th rung removes the last leak that `--no-lemmas` left behind. In
`--no-lemmas` the anchor's **proof body is still intact**, so the helper-lemma
**names + signatures leak** from its callsites (`lemma_decompress_valid_branch(…)`
etc.). `--no-anchor-proof` strips the anchor's own orchestration proof too, so
the agent must **reconstruct decompress's proof AND invent the helper lemmas
from scratch** — choosing its own decomposition (it need not reproduce
`lemma_decompress_valid_branch` at all).

**Launch** (identical contract to the other modes — only the flag changes):
```bash
DALEK_DEMO_TOKEN_FILE=/path/to/token \
  ./demo_decompress.sh --no-anchor-proof --run-id web_<ts> [--rounds 6] [--budget 90]
# stdout → RUN_ID / RESULTS / MODE / LOG / PID ; exit 0 = launched
```
Give it the most headroom of any rung — defaults bump to `--rounds 6 --budget 90`
(the agent rebuilds decompress's orchestration proof, which leans on the
`step_1`/`step_2` contracts, *on top of* inventing the lemmas, so convergence is
lower).

**What the prep does** (in `edwards.rs` **and** `decompress_lemmas.rs`):
1. `strip_specs.py --strip-proof-fn decompress` removes decompress's inline
   `proof { … }` blocks (and any proof-only `assert`s), keeping its
   **signature + `requires`/`ensures`/`decreases` byte-identical to `main`** and
   its executable body intact.
2. `strip_specs.py --delete-fn` removes the decompress lemmas now reachable
   **only** from the stripped proof — an explicit deterministic list (a fixed
   property of the pinned source): `lemma_decompress_valid_branch`,
   `lemma_decompress_field_element_sign_bit`,
   `lemma_sign_bit_after_conditional_negate`. Lemmas still referenced elsewhere
   (e.g. `montgomery.rs` → `lemma_to_edwards_correctness` →
   `lemma_decompress_spec_matches_point`) are **kept**, so the crate keeps
   compiling.

**Starting state the user sees** (the "before" pane): open
`RESULTS/snapshots/round_0/` — `edwards/edwards.rs`'s `decompress` has **0
`proof { }` blocks** (just `let`/`if`/`result`) while its `ensures` is unchanged,
and `decompress_lemmas.rs` has the decompress lemmas gone. The first
`round_1.json` shows `verus_okay:false` with the error **"postcondition not
satisfied"** at the `edwards.rs::decompress` ensures — **not** "cannot find
lemma…". That is the agent's starting signal, and the whole point of this rung.

**Editable surface.** This is the **first rung where the anchor file is
editable** (`--experiment-allow-edit` covers both `edwards.rs` and
`decompress_lemmas.rs`). The run uses `--experiment-mode contract-only` with the
spec-integrity **gate ON** (no `--no-spec-gate`).

**Why this is still a sound "COMPLETE."** Soundness comes from: (1) decompress's
signature + `requires` + `ensures` frozen and **gate-protected** (a weakened
ensures or changed signature → `SPEC_DRIFT`, terminal, never promoted to
COMPLETE), (2) no `admit()`/`assume()`/`external_body`, (3) Verus accepts with 0
admits. The fixed *proof body* was never what made it sound — only the fixed
*contract* is. So letting the agent rewrite decompress's proof is fine: whatever
proof it writes, Verus has checked the **frozen** `ensures` holds for all inputs,
with no cheats. `spec_check` freezes only the contract clauses (header +
`requires`/`ensures`/`decreases`), not the body, so an editable anchor still has
its contract protected while its proof body is free to change.

**Poll loop** is exactly the same as every other mode (`round_N.jsonl` →
activity, `round_N.json` → verus status, `snapshots/round_N/…` → diff,
`result.json` → terminal). The diff shows decompress's proof body grow back
*and* `decompress_lemmas.rs` repopulate with the agent's own lemmas.

The website adds a **"dalek · no anchor proof"** button mapping to
`--no-anchor-proof` — no other website changes needed (same handoff + results
contract).

## Concurrency: the worktree pool

`run.py` is single-target and `demo_decompress.sh` defaults to **one** shared
worktree. Two simultaneous calls on the same worktree clash on the dep file and
the cargo lock → corrupted results. The fix is a **pool of isolated, pre-warmed
worktree slots**, with the website owning a K-slot semaphore.

**One-time setup** (`demo_pool_setup.sh`) creates K slots and prints them as
JSON:
```bash
./demo_pool_setup.sh --size 3 --pool-dir /tmp/dalek-demo-pool
# → [ {slot, project, gitroot, results}, ... ]   (each warmed, ~40-90s each)
```
For full website↔local decoupling, point `--gitroot` at a **dedicated clone**
of the dalek repo (otherwise a local `git commit`/branch-switch on the shared
clone ripples into every slot — refs are shared across worktrees).

**Per request**, the website (a) leases a free slot from its semaphore, then
(b) passes that slot's triple to `demo_decompress.sh`:
```bash
DALEK_PROJECT=<slot.project> DALEK_GITROOT=<slot.gitroot> \
DALEK_RESULTS=<slot.results>  DALEK_DEMO_TOKEN_FILE=<token> \
  ./demo_decompress.sh --no-lemmas --run-id web_<ts>
# RESULTS line now points under <slot.results>/web_<ts>/edwards
```
Release the slot back to the semaphore when `result.json` appears (or the run's
PID exits).

What each layer isolates:
- **slot worktree** → dep file + cargo `target/` lock (the corruption-critical races)
- **`DALEK_RESULTS`** → the cumulative `catalog_cache`/`failure_memory`/`proven_registry` JSON
- **per-run-id dirs** (already) → `result.json` / `round_N` / `snapshots`

**K bounds** simultaneous in-flight runs (excess queue on the semaphore). It is
*not* an isolation knob; cap it by disk (K × ~1-3 GB), z3/CPU, and — the real
ceiling — the single account's rate limit (all slots bill one token; too-high K
→ 429 → `RATE_LIMITED`). K=3 on one account is a sane start.

**Safety interlock (defense in depth).** Even if the semaphore has a bug,
`demo_decompress.sh` takes an exclusive per-worktree lock and **exits nonzero
"worktree busy"** rather than clobbering a tree with a live run; a crashed
holder (dead pid) is auto-reclaimed. So a double-book is a loud error, never
silent corruption.

## Caveats (read before going live)
- **Failure memory.** `run.py` injects prior-run failures for the same
  module into the prompt. For a clean-room demo each time, add
  `--no-failure-memory` to the `run.py` argv in `demo_decompress.sh`.
- **Budget.** `--rounds`/`--budget` cap the run; on exhaustion `result.json`
  ends `LIMIT`, not `COMPLETE`. Opus solved both shapes in 1 round at
  `--rounds 4 --budget 45`.
- **Token.** The OAuth token bills a specific account and must be supplied via
  env/file (never commit it). Rotate it if it has been exposed.
