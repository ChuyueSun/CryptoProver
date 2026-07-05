# Spec-gen runbook: surgically strip a proof, then run the experiment

A hands-on guide for running the **spec-gen** (spec-strip / proof-reconstruction)
experiments. The goal of an experiment: take a clean, fully-proven
curve25519-dalek module, **surgically remove one slice of its proof**, freeze
everything else, and have a `claude -p` agent reconstruct the missing slice
against **frozen specs** — so we measure proof-reconstruction capability while
*guaranteeing the agent cannot weaken the user-facing contract*.

If you only read one thing: **use peel mode** — [`peel_manifests/`](../peel_manifests/)
+ [`peel.py`](../peel.py) + [`peel_run.sh`](../peel_run.sh). A peel manifest
describes one experiment as data (which shells to peel, which mode, which pin);
`peel_run.sh` builds the peeled worktree and launches `run.py` in one command.
**§1 below is the whole coauthor guide.** The older launchers
([`launch_specgen.sh`](../launch_specgen.sh), [`demo_decompress.sh`](../demo_decompress.sh))
and the raw `strip_specs.py`/`admit.py` verbs still work and are documented from
§2 on as the lower-level / by-hand layer peel composes.

Companion docs (read these for *why*, this doc is *how*):
- [`peel_manifests/README.md`](../peel_manifests/README.md) — the rung↔manifest
  mapping table (which manifest reproduces which old `--experiment-mode` rung).
- [`docs/spec_gen_experiment_design.md`](spec_gen_experiment_design.md) — the layer
  model (L1–L5), which `pub fn`s are valid contract anchors, the design rationale.
- [`docs/website_backend.md`](website_backend.md) — the difficulty-rung ladder and
  the website-demo launcher (`demo_decompress.sh`).
- [`CLAUDE.md`](../CLAUDE.md) → *Branch-local additions* — the canonical CLI reference.

---

## Coauthor quickstart (TL;DR)

You don't need to understand peel depth or the proof stack to run an experiment.
Pick a manifest, wire up three prereqs, and launch. Full detail for each step is
linked.

**1. Toolchain + auth (one-time per machine — [§2](#2-prerequisites-one-time-per-machine)).**
`run.py` shells out to `claude`, `cargo verus`, `z3`, `python3` — none on `PATH`
by default. Export your machine's prelude, then sanity-check:

```bash
export PATH="…/cpython-3.11+/bin:…/verus-<rev>/bin:$PATH"   # your paths; see §2
command -v python3 cargo-verus claude          # all three must resolve
```

The spawned `claude -p` does **not** inherit the desktop app's login. Provide one of:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=<claude-oauth-token>  # `claude setup-token` to mint one
# …or do a keychain `claude` → /login once; the headless process picks it up.
```

> Verus is version-pinned to the repo: use the prebuilt release matching the
> `rev` in `curve25519-dalek/Cargo.toml` (`[package.metadata.verus]`), **not** a
> locally-built verus from another rev — mismatched macros fail to compile.

**2. Clone the pinned Rust source** (the experiment resets files to a clean git ref):

```bash
git clone https://github.com/ChuyueSun/dalek-lite.git dalek-lite   # the git ROOT
cd dalek-lite
git fetch origin corefloor-base-103b92b9
# DALEK_SRCREPO = this repo root; peel_run.sh makes its own worktree from it.
# For exact benchmark reproduction, set DALEK_SRCREF explicitly rather than
# relying on a moving branch name:
#   DALEK_SRCREF=d74d68927edf7d08b1e4c977550eeb381fe15326  # ChuyueSun main baseline
#   DALEK_SRCREF=corefloor-base-103b92b9      # generated field-floor/deeper-floor base
```

**3. Surface the cut (no worktree, no launch, no cost) — always do this first:**

```bash
./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json --surface
```

It prints exactly which files become editable and the per-file op. If that
matches your intent, run it for real:

```bash
DALEK_SRCREPO=/path/to/dalek-lite \
  ./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json \
      --run-id peel_bf_001 --rounds 10 --budget 180 --detach
tail -f launcher_peel_peel_bf_001.log        # MARKER lines = progress
./peel_run.sh --run-id peel_bf_001 --remove   # tear down the worktree when done
```

**Which manifest?** Five ready-made rungs in increasing difficulty —
`proof_only` → `contract_only` → `bridge_specs` → `bridge_full` → `fullstack`.
Difficulty + recommended `--rounds`/`--budget` per rung are in
[`peel_manifests/README.md`](../peel_manifests/README.md). Start with
`decompress_proof_only.json` (4 rounds / 45 min) to confirm the pipeline works
end-to-end before committing to a long run.

That's the whole loop. [§1](#1-peel-mode--the-one-command-path-coauthor-start-here)
explains *what* a manifest is and how to author a new cut; §2+ is the lower-level
by-hand layer you only need to debug peel.

---

## 0. Mental model — the three verbs of a strip

Every rung is built from exactly three operations on a clean source tree. Being
precise about which verb applies to which file *is* the experiment design:

| Verb | What it removes | What survives | Tool |
|------|-----------------|---------------|------|
| **FREEZE** | nothing | the whole file, reset to clean `main`; agent may not edit it | `git checkout main --` + run.py file-guard |
| **STRIP** *(headers)* | `requires`/`ensures`/`decreases` clauses | signature, body | `strip_specs.py` (default) |
| **STRIP** *(proof)* | inline `proof { … }` blocks + standalone `assert(…)` | signature, contract, **executable** body | `strip_specs.py --strip-proof-fn` |
| **DELETE** | the entire fn (sig + contract + body + `///` docs) | nothing — callsites stop compiling | `strip_specs.py --delete-fn` |
| **ADMIT** | proof-fn *bodies* → `admit()` | signature, contract, docs | `admit.py --mode fn-bodies` |

The **anchor** is the one file/contract under test: it is always a frozen L1
**contract** (a user-facing API's `requires`/`ensures`), written in frozen L2
**spec vocabulary**. The agent reconstructs L1 *proofs* and/or L3 *lemmas*; the
L4 field layer and L5 number-theory floor are the assumed, frozen substrate.

**Soundness comes from what you freeze, not from what you strip.** As long as
every spec definition the contract is written in stays frozen, a too-weak
reconstruction merely *fails to verify* — it can never silently weaken the
guarantee. Keep that invariant in mind whenever you design a new cut.

---

## 1. Peel mode — the one-command path (coauthor start here)

Peel collapses the three verbs above into **one axis — peel depth** — and one
data file per experiment (a *manifest*). You never touch `strip_specs.py` /
`admit.py` directly; you pick a manifest and run it.

### 1.1 The peel-depth axis

Each depth removes one more shell of the proof stack, strictly cumulative
(depth N removes shells 1..N, so difficulty is totally ordered):

| Depth | Shell | Removes | Built from |
|:---:|---------|---------|------------|
| P1 | proofs   | `proof fn` bodies + inline `proof { }` blocks | `admit.py` (admit) or `strip_specs.py --strip-proof-fn` (strip) |
| P2 | lemmas   | + named helper lemmas (sig + contract + body) | `strip_specs.py --delete-fn` |
| P3 | specs    | + named `spec fn` definitions | `strip_specs.py --delete-fn` |
| P4 | contract | + `requires`/`ensures`/`decreases` off named anchors | `strip_specs.py` (default strip) |

The **frozen floor** — exec code + every `axiom_*` / `assume` / `external_body`
— is never peeled. Shell P has two *non-interchangeable* variants set per file
by `proof_op`: `admit` (GREEN start — every obligation trivially discharged,
verus passes, whole-file) vs `strip`/`strip-all` (RED start — postcondition
unproven, verus fails, exec + contract kept). `none` leaves proofs intact (the
P4 proof-pin).

> **The pin rule.** A cut is *self-pinning* — the agent cannot pass by weakening
> the guarantee — only while every artifact the frozen contract is phrased in
> stays frozen. P1–P3 keep the top contract frozen, so they self-pin. **P4**
> (strips the contract) and any **P3 that deletes a `spec fn`** (the contract is
> now phrased in an agent-reconstructed definition) do NOT self-pin, so peel
> *refuses to build them without a declared `--pin`*: `proof` (a retained
> frozen-and-proven consumer), `consumer:NAME`, or `oracle:REF`. The depth axis
> deliberately has **no auto-map to experiment-mode** — the mode is declared in
> the manifest, because "strip stratum k, keep 1..k-1 as the pin" is not a
> function of depth (see the long NOTE in [`peel.py`](../peel.py)).

### 1.2 A manifest

A manifest is the experiment as data (see
[`peel_manifests/README.md`](../peel_manifests/README.md) for the key reference).
Example — the `bridge-full` decompress rung (= the old `--no-bridge-lemmas`):

```jsonc
{
  "name": "decompress-bridge-full",
  "depth": 2,                              // peel proofs + delete the listed lemmas
  "experiment_mode": "bridge-full",        // → run.py --experiment-mode (NOT inferred)
  "pin": "proof",                          // recorded; enforced for P4 / spec-deletes
  "target": "curve25519-dalek/src/edwards.rs",   // the run.py anchor
  "files": [                               // every listed file is EDITABLE; rest frozen
    {"path": ".../decompress_lemmas.rs", "proof_op": "none",
     "lemmas": ["lemma_decompress_valid_branch", ...]},   // delete these 5
    {"path": ".../curve_equation_lemmas.rs", "proof_op": "none",
     "lemmas": ["lemma_negation_preserves_curve", ...]}   // delete these 5
  ]
}
```

Five ready-made manifests reproduce the frozen-contract decompress rungs exactly
(`decompress_proof_only` → `..._contract_only` → `..._bridge_specs` →
`..._bridge_full` → `..._fullstack`); the mapping to the old rungs is the table
in [`peel_manifests/README.md`](../peel_manifests/README.md).

### 1.3 Run one

```bash
# Preview the cut — no worktree, no launch (paths it will edit + per-file ops):
./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json --surface

# Build the peeled worktree + show the run.py argv, but DON'T launch:
DALEK_SRCREPO=/path/to/dalek-lite \
  ./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json \
      --run-id peel_bf_001 --dry-run

# For real, detached (survives the terminal / Claude Code teardown):
DALEK_SRCREPO=/path/to/dalek-lite \
  ./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json \
      --run-id peel_bf_001 --rounds 10 --budget 180 --detach
# → RUN_ID / RESULTS / MODE / LOG / PID printed; then:
tail -f launcher_peel_peel_bf_001.log

# Tear the worktree down when done:
./peel_run.sh --run-id peel_bf_001 --remove
```

`peel_run.sh` is the **thin bridge**: it runs `peel.py --worktree` (which checks
out `--ref`, default `main`, into a fresh per-`run-id` worktree and applies the
manifest), reads `project` + `experiment_mode` + `editable_files` straight out of
peel's JSON, and invokes `run.py --experiment-mode <mode> --experiment-allow-edit
<files…>`. It adds `--no-spec-gate` automatically for `spec-proof`. It does *not*
reset-in-place — each `run-id` gets its own fresh worktree. Fresh worktree
creation is serialized per source repo because git metadata is shared; once
launched, runs can proceed in parallel if each has its own `DALEK_RESULTS`.

`--depth` is taken from the manifest's `depth` key; pass `--depth N` to override.
`--ref` defaults to `main` (peel strips proofs itself, so it wants clean proven
source). Env vars (`DALEK_SRCREPO`, `DALEK_PEEL_WT_BASE`, `DALEK_VSTD`,
`DALEK_RESULTS`, toolchain + auth) match `demo_decompress.sh` — see the header of
[`peel_run.sh`](../peel_run.sh) and the machine prelude in §2 below.

### 1.3b Containerized parallel sweep (GCP VM) — `docker/`

> **On `main`:** the `docker/` toolkit below is merged (`be10128`) and internally reviewed (thread T112). VM-smoke-
> tested on the benchmark VM (rustc 1.92.0): image build + seal + launcher
> plumbing + a real single-container `run.py` proof round all run (the sample task
> ended `PROCESS_CROSSTALK` — a harness gate, not a Docker issue). A full *parallel*
> sweep is still unrun. Treat it as the recommended path for
> *parallel* sweeps; for a single run, `peel_run.sh` above is the one-command path.

`peel_run.sh` is sequential per source worktree (shared cargo lock +
`failure_memory.json`/`proven_registry.json` races). To fan out **one agent per
target in parallel** while keeping each agent's worktree isolated *and* sharing one
CPU pool, use [`docker/run_agents.sh`](../docker/run_agents.sh) — the containerized
counterpart of `peel_run.sh`:

```bash
# one container per manifest line; CPU shared via CFS, worktrees isolated
export CLAUDE_CODE_OAUTH_TOKEN=...           # memory: headless auth
docker/run_agents.sh --image dalek-harness:v1 \
    --gitroot /path/to/dalek-lite --ref eval/admitted-start \
    --run-id sweep_001 --manifests-file /tmp/manifests.txt
# manifests.txt: one peel manifest per line  ->  <manifest.json> [| pin | depth | minutes]
```

Each container gets an **isolated-store sealed `/work`** (no shared git store → no
ground-truth oracle), its own `/results`, and inherits a baked-warm offline
`CARGO_HOME`/`CARGO_TARGET_DIR` via image COW. CPU is shared (no `--cpus`/`--cpuset`;
equal `--cpu-shares`, concurrency capped near `nproc`). Full design + build/preflight
steps: [`docker/README.md`](../docker/README.md).

### 1.4 Author a new cut

1. Copy the nearest `peel_manifests/*.json`, edit the file list / names.
2. `--surface` it — confirm the editable set and per-file ops are what you mean.
3. For a whole-*directory* cut (not a hand-named lemma list), generate the
   manifest instead of writing it: `python3 peel.py --classify
   /path/to/wt/curve25519-dalek` (the `--strip-to-fields` field-floor cut), then
   add `depth`/`experiment_mode`/`pin`/`target` and run it. Pass
   `--classify-floor number-theory|trusted-core` (default `field`) to cut a
   deeper, broader cone — a **monotone superset ladder** (235 → 504 → 815
   lemmas, all still depth 2 / `pin: proof`). There is **no separate
   `--experiment-mode` for the deeper floors**: all declare
   `experiment_mode: "field-floor"` and `run.py` detects the variant from the
   editable set. The prompt wording is variant-specific: trusted-core still
   freezes existing spec definitions and public contracts, but editable
   `specs/*` files may receive new real helper `proof fn` lemmas (no
   `external_body`, `assume`, `admit`, new `axiom_*`, or spec-definition edits).
   Full coauthor-runnable recipe (classify from the proven ref
   `corefloor-base-103b92b9` — *not* the unborn local `main` in
   `/tmp/dalek-baf` — surface, run, plus the tap-port-collision caveat) is in
   [`peel_manifests/README.md`](../peel_manifests/README.md) → "The deeper
   floors".
4. The deterministic transform is pinned by `tests/test_peel.py`
   (`python3 -m unittest tests.test_peel`, Python 3.11+) — if you extend the
   shell ops, extend it.

Everything from §2 on is the lower-level layer (machine setup, the by-hand verbs,
the legacy launchers). You only need it to debug peel or craft a cut peel can't
yet express (the gate-OFF `spec-proof` rungs — see the runbook §7 mapping).

---

## 2. Prerequisites (one-time per machine)

`run.py` shells out to `claude`, `cargo verus`, `z3`, and `python3` — none of
which are on `PATH` by default on this machine. The launcher scripts bootstrap
all of it; if you run the tools by hand, export this prelude first:

```bash
export PATH="/path/to/python3/bin:/tmp/verus-rel/verus-arm64-macos:$PATH"
command -v python3 cargo-verus claude   # all three must resolve
```

(Override any path via the `DALEK_*` env vars in §6. On a different machine, set
them to your toolchain.)

**Auth.** The spawned `claude -p` does *not* inherit the desktop app's OAuth.
Provide one of:
- `export CLAUDE_CODE_OAUTH_TOKEN=…` (or `DALEK_DEMO_TOKEN_FILE=/path/to/tokenfile`), or
- a keychain login via `claude` `/login` once, which the headless process picks up.

---

## 3. Get a clean, git-backed worktree (the starting state)

**`launch_specgen.sh` now owns this for you** — see *Clean start* below. You only
need to do it by hand when driving `run.py` directly (§7).

Every rung resets its files to a clean, fully-proven ref, so the experiment
**requires a git-backed checkout of the dalek project at that ref** (default
`main`). A plain directory copy fails at the first reset with `fatal: not a git
repository`.

### Clean start (automatic in `launch_specgen.sh`)

Before any strip, the launcher guarantees a pristine worktree, so a run never
inherits a prior reconstruction or a half-broken tree:

| `$DALEK_GITROOT` state | What happens |
|---|---|
| valid worktree at `$DALEK_SRCREF` | **hard-reset** the member to the ref + drop stray untracked files |
| missing | `git worktree add` it from `$DALEK_SRCREPO` @ ref |
| present but broken (no/corrupt `.git`) | re-run with **`--bootstrap`** → `rm -rf` + recreate (guarded so it never nukes a tree you didn't make) |
| no usable source | dies with the exact command to create one |

So the first run on a fresh/broken machine is:

```bash
DALEK_SRCREPO=/path/to/dalek-lite ./launch_specgen.sh --strip-to-fields \
    --run-id sg_001 --bootstrap        # heals $GITROOT, then runs
# every subsequent run needs no --bootstrap — it just hard-resets clean:
./launch_specgen.sh --strip-to-fields --run-id sg_002
```

`DALEK_SRCREF` is the clean proven ref; set it explicitly for reproducible
benchmarks. Use the pinned `ChuyueSun/dalek-lite` source repo, not upstream
BAF@moving-main. For the generated field-floor / deeper-floor cuts, use
`corefloor-base-103b92b9`; for older main-based decompression rungs, use the
pinned ChuyueSun main commit documented in `docs/diagnostics.md`.

### By hand (only for direct `run.py` use)

The dalek project is a Cargo **workspace**: the git root holds the workspace
`Cargo.toml`; the `curve25519-dalek/` member subdir is what you pass as
`--project`. Create an isolated worktree with [`admit.py`](../admit.py):

```bash
REPO=/path/to/dalek-lite                 # the dalek project git repo root
# detached worktree at the already-proven main (NOT the admitted ref — spec-gen
# strips proofs itself; it wants clean proven source to reset to):
python admit.py --worktree /private/tmp/dalek-spec-strip --gitroot "$REPO" --ref main
# → {"project": "/private/tmp/dalek-spec-strip/curve25519-dalek", ...}
```

Or by hand: `git -C "$REPO" worktree add --detach /private/tmp/dalek-spec-strip main`.

> **The `main` ref must exist and be the clean proven tree.** Verify before you
> rely on it: `git -C /private/tmp/dalek-spec-strip rev-parse main` and
> `cargo verus verify -p curve25519-dalek` from the member dir should pass
> 2000-odd / 0.

**Warm vstd once** on a brand-new worktree (the first module-scoped check
spuriously fails on a cold build):

```bash
( cd /private/tmp/dalek-spec-strip/curve25519-dalek && cargo verus verify -p curve25519-dalek )  # ~40s
```

`launch_specgen.sh` does this warm automatically (sentinel
`target/.specgen_warmed`; skip with `--skip-warm`).

When you're done: `python admit.py --worktree /private/tmp/dalek-spec-strip --gitroot "$REPO" --remove`.

> ✅ **Machine state (2026-06, healed):** the local source checkout path
> `/private/tmp/dalek-baf` was recovered into a valid repo at the chosen clean
> source ref (it had a corrupt `.git` missing `HEAD`), and
> `/private/tmp/dalek-spec-strip` was rebuilt as a fresh worktree from it. The
> historical directory name may still say `dalek-baf`; for reproducible
> benchmark inputs, make sure that checkout is the pinned `ChuyueSun/dalek-lite`
> source/ref above. The launcher defaults (`DALEK_SRCREPO=/private/tmp/dalek-baf`)
> now self-heal:
> a normal run hard-resets clean; a broken `$GITROOT` re-creates on `--bootstrap`.
> `--print-surface` works without any worktree; launching needs one (auto-built).

---

## 4. The rungs (and which one to use)

`launch_specgen.sh` exposes ten rungs, increasing in difficulty. The first nine
are the **decompress ladder** (a single proof anchor, `edwards.rs::decompress`,
with progressively more stripped); they're kept for reference and the website
demo. **The rung in active use is `--strip-to-fields`** — the maximal cut.

| Flag | Anchor | Strips | Agent rebuilds |
|------|--------|--------|----------------|
| `--formal-spec` | edwards decompress | admit dep lemma bodies | proofs |
| `--no-spec` `[--strip-docs]` | edwards decompress | dep lemma headers + bodies | contracts + proofs |
| `--no-lemmas` | edwards decompress | delete every dep lemma | invents the helpers |
| `--no-anchor-proof` | edwards decompress | anchor proof body + 3 helpers | anchor orchestration + helpers |
| `--no-bridge-specs` | edwards decompress | the 2 Mont↔Edw map spec fns | the map definitions |
| `--no-bridge-lemmas` | edwards decompress | 10 decompress-path lemmas (map frozen) | the decompress lemma tree |
| `--no-api-proof` | edwards decompress | + edwards/montgomery API proof bodies | API proofs + 10 lemmas |
| `--no-ristretto-proof` | ristretto decompress | ristretto decompress+step_1+step_2 proofs | the ristretto proof layer |
| `--no-fullstack-proof` | ristretto decompress | all 3 API proofs + 10 lemmas | the whole decompress tree |
| **`--strip-to-fields`** | **ristretto** | **every non-axiom proof above the field layer + all API inline proofs** | **the entire above-field proof tree** |

Inspect any rung's exact surface without touching the tree:

```bash
./launch_specgen.sh --strip-to-fields --print-surface
./launch_specgen.sh --no-bridge-lemmas --print-surface
```

---

## 5. The default rung: `--strip-to-fields`

> **Freeze everything reachable from the user-facing API contracts down to the
> field floor; delete every proof above that floor.** The agent reconstructs the
> entire above-field proof tree (all L3 correctness lemmas + the API orchestration
> proofs) from frozen contracts + frozen spec vocabulary + the frozen field
> substrate alone.

The cut, at directory granularity (paths relative to `src/`):

```
DELETE  every non-axiom `proof fn`  (→ EDITABLE; agent rebuilds)
          lemmas/edwards_lemmas/        (~143 proofs)
          lemmas/ristretto_lemmas/      (~25; axioms.rs excluded & frozen)
          lemmas/scalar_lemmas_/        (~50)
          lemmas/scalar_byte_lemmas/    (~12)
STRIP   inline proofs, keep contract + exec  (→ EDITABLE)
          edwards.rs  montgomery.rs  ristretto.rs  scalar.rs
FREEZE  reset to main + file-guard  (agent must not touch)
          specs/   lemmas/field_lemmas/   lemmas/common_lemmas/   backend/
          + every axiom_*   (axioms are unprovable — cannot be rebuilt)
```

Mode is `bridge-full` (whole-crate verify + frozen-file guard each round), gate
**ON** (every editable file's fn headers are snapshotted; any contract edit ⇒
`SPEC_DRIFT`), target `ristretto.rs` (the topmost anchor).

### Spec-definition freeze (closes the co-location gap)

This rung's editable lemma/API files **contain spec vocabulary co-located with
proofs** (e.g. `edwards_lemmas` has ~27 `open spec fn`s; `edwards.rs` has 17).
A frozen `ensures` like `is_well_formed_edwards_point(result)` is written in
that vocabulary, so redefining the spec fn's **body** would hollow out the
contract without touching any clause — and the header-only spec gate would miss
it (a *weaker* spec makes the frozen proof *easier*, so whole-crate verify still
passes).

That gap is **closed by the spec-definition gate**: whenever the spec gate is on
(every mode except `spec-proof`), `run.py` runs `spec_check verify
--check-spec-defs`, which snapshots **every existing spec fn's body** and fails
the round as `SPEC_DRIFT` on any change. New spec helpers are still allowed;
existing vocabulary is structurally frozen — so "everything reachable from the
contract" is frozen by construction, the same guarantee the curated rungs get
from keeping spec fns in separate frozen files. (This also retroactively hardens
`--no-api-proof` / `--no-fullstack-proof`, whose editable API files had the same
latent co-location.) You should still spot-check spec bodies in an audit (§8),
but the gate now catches a weakening during the run, not just after.

---

## 6. Run it

### Quickstart

```bash
# 1. inspect the cut (no worktree needed)
./launch_specgen.sh --strip-to-fields --print-surface

# 2. dry-run: reset + strip the worktree, print the run.py argv, DON'T launch
./launch_specgen.sh --strip-to-fields --run-id sg_smoke --dry-run

# 3. for real, detached (survives the terminal / Claude Code teardown)
./launch_specgen.sh --strip-to-fields --run-id sg_001 --detach
# → RUN_ID / RESULTS / LOG / PID printed; tail the log:
tail -f launcher_specgen_sg_001.log
```

Foreground (interactive shell, short runs) — drop `--detach`; the script
`exec`s `run.py`, so its exit code is the proof outcome (see §9).

### Flags

```
--run-id ID        required to launch (omit only with --print-surface)
--rounds N         per-rung default; --strip-to-fields defaults to 20
--budget MIN       wall-clock cap (--max-task-minutes); default 240
--model M          opus (default) | sonnet | haiku | claude-sonnet-4-6
--print-surface    print the strip surface and exit (no reset, no launch)
--dry-run          apply the strip to the worktree + show argv, but don't launch
--detach           re-exec via setsid so the run survives a process-group kill
--skip-warm        skip the one-time vstd warm
--bootstrap        if $GITROOT exists but is broken, rm -rf and recreate it from
                   $DALEK_SRCREPO (required only to replace a non-worktree dir)
--strip-docs       (only with --no-spec) also strip the /// proof-sketch docs
```

### Env overrides (defaults are machine-specific; same names as `demo_decompress.sh`)

| Var | Meaning | Default |
|-----|---------|---------|
| `DALEK_PROJECT` | Cargo member dir (the `--project`) | `/private/tmp/dalek-spec-strip/curve25519-dalek` |
| `DALEK_GITROOT` | workspace git root (reset target) | `/private/tmp/dalek-spec-strip` |
| `DALEK_VSTD` | vstd source dir | `…/verus…/source/vstd` |
| `DALEK_VERUS_DIR` / `DALEK_UV_PY_BIN` | toolchain dirs prepended to `PATH` | machine paths |
| `DALEK_RESULTS` | results root | `<harness>/results` |
| `CLAUDE_CODE_OAUTH_TOKEN` / `DALEK_DEMO_TOKEN_FILE` | headless auth | keychain fallback |

Point at a different worktree:

```bash
DALEK_PROJECT=/my/wt/curve25519-dalek DALEK_GITROOT=/my/wt \
  ./launch_specgen.sh --strip-to-fields --run-id sg_002 --detach
```

> **One sweep per worktree.** A single project worktree serializes on the cargo
> lock and races on the cumulative `failure_memory.json` / `proven_registry.json`.
> To run two rungs at once, give each its **own worktree AND its own
> `DALEK_RESULTS`**. Fresh worktree creation should still be serialized; use
> `peel_run.sh` rather than hand-rolled concurrent `git worktree add` calls.

---

## 7. Strip surgically by hand (when the launcher isn't enough)

`launch_specgen.sh` resets to `main` then calls these primitives. To craft a
bespoke cut, do the same two steps yourself. **Always reset to clean `main`
first** — the strip passes reset any proof already present, so they must run on a
pristine file.

```bash
P=/private/tmp/dalek-spec-strip/curve25519-dalek
G=/private/tmp/dalek-spec-strip
reset() { git -C "$G" checkout main -- "$(python3 -c 'import os,sys;print(os.path.relpath(*sys.argv[1:]))' "$1" "$G")"; }
```

**Strip a fn's inline proof, keep its contract + exec** (the `--no-anchor-proof`
move):
```bash
reset "$P/src/edwards.rs"
python3 strip_specs.py "$P/src/edwards.rs" --in-place --strip-proof-fn decompress
```

**Delete whole lemmas** (callsites then fail to compile until rebuilt):
```bash
reset "$P/src/lemmas/edwards_lemmas/decompress_lemmas.rs"
python3 strip_specs.py "$P/src/lemmas/edwards_lemmas/decompress_lemmas.rs" --in-place \
    --delete-fn lemma_decompress_valid_branch \
    --delete-fn lemma_to_edwards_correctness
```

**Strip fn-header contracts** (the `--no-spec` move; add `--strip-docs` to also
drop the `///` sketch), then admit the bodies:
```bash
python3 strip_specs.py "$DEP" --in-place           # strips every fn's requires/ensures
python3 admit.py       "$DEP" --in-place --mode fn-bodies
```

**Delete every non-axiom proof in a file** (the `--strip-to-fields` move — keeps
`axiom_*` and `spec fn`s):
```bash
args=( "$F" --in-place )
while read -r n; do case "$n" in axiom_*) continue;; esac; args+=( --delete-fn "$n" ); done < <(
    grep -oE 'proof fn [a-zA-Z0-9_]+' "$F" | awk '{print $3}')
python3 strip_specs.py "${args[@]}"
```

Then launch `run.py` directly. The mode → flag mapping:

| Rung kind | `--experiment-mode` | gate |
|-----------|---------------------|------|
| proofs only, contracts given | `proof-only` | ON (no flag) |
| agent writes contracts | `spec-proof` | OFF (`--no-spec-gate`, auto-set) |
| anchor proof + helpers, contract frozen | `contract-only` | ON |
| reconstruct shared spec fns | `bridge-specs` | ON + whole-crate verify + frozen-file guard |
| pure proof reconstruction, all specs frozen | `bridge-full` | ON + whole-crate verify + frozen-file guard |

```bash
python3 run.py "$P/src/ristretto.rs" --project "$P" --run-id sg_manual \
    --rounds 20 --max-task-minutes 240 --model opus \
    --vstd-root "$DALEK_VSTD" --results <harness>/results \
    --experiment-allow-edit <editable files…> --experiment-mode bridge-full
```

`spec-proof` auto-enables `--no-spec-gate` (the agent rewrites contracts, so the
snapshot gate would always fail). Every other mode keeps the gate ON.

---

## 8. Inspect results & audit a COMPLETE

State lives as JSON under `results/<run_id>/<target_id>/`
(`target_id` = the target file stem, e.g. `ristretto`):

```bash
RD=results/sg_001/ristretto
jq . "$RD/result.json"                       # end_reason, verus_okay, admits, cost, duration
python3 replay.py "$RD/claude_raw/round_1.jsonl"          # pretty-print the agent stream
python3 replay.py "$RD/claude_raw/round_1.jsonl" --only tool_use
find "$RD/claude_memory" -maxdepth 2 -type f  # Claude auto-memory snapshots, if present
tail -f "$RD/cli.log"                          # live skill-call trace
```

`result.json` `end_reason` values: **`COMPLETE`** (the final-state gate proves
Verus is okay for the configured scope, no integrity gate drifted, and zero hard
`admit()` remain in scope; it can promote an over-cautious `LIMIT` / empty
claim), `SPEC_DRIFT` / `AXIOM_DRIFT` / `TOOLING_DRIFT` / `FROZEN_EDIT` (an
integrity gate tripped — non-promotable), `LIMIT` (budget), `NEEDS_DECOMP`
(agent declared missing infrastructure), `RATE_LIMITED` (HTTP 429; `run.py`
exits **42**), `RETRY_EXHAUSTED` (Claude exhausted retry budget with no result
event), `USER_INTERRUPTED` (Claude stream explicitly reports a user/tool
interruption), and `TRANSPORT_ERROR` (other positive nonzero Claude exit with no
result event).

For whole-crate experiment modes (`field-floor`, `bridge-specs`,
`bridge-full`), the target path is a **harness anchor**, not the full task
scope. The assignment, edit scope, and hard-admit count are the
`--experiment-allow-edit` list. Use `verus_check.py --whole-crate` and read its
structured `summary` / `messages[]`; `stderr_tail` is truncated context, and
the skill's `error_count` is a diagnostic-message count, not the same
denominator as Verus's progress line (`N verified, M errors`).

**Auditing a COMPLETE is mandatory**, especially for `--strip-to-fields` (see the
§5 caveat). Confirm independently:

```bash
# whole crate verifies at the harness standard rlimit
( cd "$P" && cargo verus verify -p curve25519-dalek --rlimit 80 )    # expect N / 0

# no cheating primitives crept in
grep -rnE '\b(admit|assume)\(' "$P/src"                    # expect: none in editable files
git -C "$G" diff --stat main                               # expect: ONLY the editable files changed

# the contract-integrity check: every frozen surface byte-identical to main
git -C "$G" diff main -- '*/specs/*'                       # expect: empty
# and for --strip-to-fields specifically — diff the co-located spec fn BODIES:
git -C "$G" diff main -- '*/lemmas/edwards_lemmas/*' | grep -A3 'spec fn'   # eyeball: defs unchanged
```

A genuine COMPLETE: whole crate verifies at rlimit 80; all API contracts + every
spec fn + all exec code byte-identical to `main`; only editable files changed;
zero `admit`/`assume`/new-`axiom_`/new-`spec fn`/git-recovery.

> **Resource artifact, not a weakening (documented precedent):** a more-compact
> reconstruction can tip an *unrelated, byte-identical* function over Verus's
> *default* rlimit (~10) even though the crate passes at rlimit 80. That's a
> crate-level resource effect, not a soundness issue — always audit at rlimit 80,
> the harness standard. See commit `12f5b39` (fullstack_proof_001).

---

## 9. Exit codes & integrity gates

**Exit codes** (foreground `launch_specgen.sh` / `run.py`): `0` ok, `1` proof
failed, `42` rate-limited (429 — re-run after the window reopens). Detached
launch returns `0` once *launched*; read the real outcome from `result.json`.

**Integrity gates** — each snapshots a baseline before the loop and fails the
round on drift (folded into the final-state gate, so even a budget-bail can't be
promoted to COMPLETE). Don't relax these:
- **Spec** (`SPEC_DRIFT`): any fn-header / `requires` / `ensures` / `decreases` /
  `external_body` change on a snapshotted file — **and**, when the gate is on
  (`--check-spec-defs`, auto for every mode except `spec-proof`), any change to
  an existing `spec fn`'s **body** (its definition). New spec fns stay allowed.
- **Axiom** (`AXIOM_DRIFT`): any new `axiom_*` name.
- **Tooling** (`TOOLING_DRIFT`): any edit to `skills/` or `lib/` (the agent shares
  this repo as cwd under `bypassPermissions` and *could* doctor a verifier).
- **Frozen-file** (`FROZEN_EDIT`, bridge modes): any edit outside the
  `--experiment-allow-edit` set; paired with a whole-crate verify each round.

The prompt also forbids `#[verifier::external_body]`, `assume(...)`, and new
`admit()`.

---

## 10. Checklist for a clean run

1. `command -v python3 cargo-verus claude` all resolve (§2 PATH prelude).
2. Auth set (`CLAUDE_CODE_OAUTH_TOKEN` / token file / keychain).
3. Worktree is **git-backed**, `main` is clean & proven, vstd warmed (§3).
4. `--print-surface` shows the cut you intend; `--dry-run` applies it cleanly.
5. Launch detached; tail `launcher_specgen_<run_id>.log`.
6. On COMPLETE: **audit** per §8 (whole-crate verify @ rlimit 80 + frozen-surface
   diff) — never trust the label alone, especially for `--strip-to-fields`.
7. One sweep per worktree; separate `DALEK_RESULTS` for parallel runs.
