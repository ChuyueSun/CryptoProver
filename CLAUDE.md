# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coordinating with the concurrent Codex agent (READ FIRST)

This worktree may be shared with a concurrent **Codex** agent. Coordinate
through the gitignored ledger **`AGENT_DEBATE.md`** at the repo root:

1. **Read `AGENT_DEBATE.md` at the start of every turn**, before editing any
   file another thread has `LOCK`ed or is debating.
2. **Append** signed posts (never edit/delete existing ones):
   `[claude HH:MM] <KIND>: <body with file:line evidence>`.
3. In any file the other agent has `LOCK`ed or is debating, don't finalize until
   you've posted `LGTM` or an `OBJECT` with a reason; that is the blocking
   mutual-review gate.
4. If you've never acked the channel, append an `LGTM` to thread **T0**.

Codex reads the file at the start of each turn, and either agent may poll the
file mtime for automatic wakeups.

## What this repo is

A slim Verus proof-synthesis agent for the dalek-lite (curve25519-dalek) Rust codebase. The agent's job is to replace `admit()` calls in Verus-annotated Rust files with real proofs that pass `cargo verus`. The MVP target was ~1k LOC of Python, small enough to read in one sitting; the `spec_gen` research branch has grown far past that (currently ~10k LOC across `run.py` + `skills/` + `lib/`) by bolting on experiment-mode and session auto-reset — see **Branch-local additions** below for what was added on top of the MVP and why.

Two specs anchor the design and define what is and isn't in scope:
- `docs/mvp_spec.md` — what's in the MVP and why
- `docs/extension_spec.md` — five deferred features, each with a documented "trigger" (the symptom that would justify building it). Don't build these on speculation.

## Commands

### Running a single target

```bash
python run.py <path/to/target.rs>                    # default: 5 rounds, auto-detect Cargo root
python run.py <target> --rounds 5 --run-id my_run
python run.py <target> --model sonnet                # haiku | sonnet | opus | claude-sonnet-4-6
python run.py <target> --vstd-root /path/to/verus/vstd  # index vstd into the catalog
python run.py <target> --max-task-minutes 30        # explicit wall-clock cap
python run.py <target> --admitted-ref eval/admitted-layerA-debug --truth-ref main  # emit diff.md
```

The target file must live inside a buildable Cargo project (an ancestor has `Cargo.toml`). `run.py` auto-detects it; pass `--project` to override.

When `--max-task-minutes` is omitted the budget auto-scales: `max(20, 1.5 * num_admits)` minutes. SIGKILL fires on the entire claude process group at the deadline — claude spawns descendants (cargo verus, z3, Monitor poll loops) that won't die otherwise.

### Running a layer set (multiple targets sequentially)

```bash
python run_layer.py A --project /path/to/curve25519-dalek --rounds 5
python run_layer.py A --project ... --run-id layerA_001 --skip-existing
```

Layer Sets A/B/C/D are all wired in (mirrored from `inference-dalek/inference_dalek/eval/domain_layers.py`): A = field repr + reduce (9 modules), B = serialize (6), C = edwards base + ops (15), D = ristretto (5). `run_layer.py` is sequential — for parallelism, fan out `run.py` invocations with `xargs -P` (each writes to its own per-task dir).

### Running an arbitrary list of targets (use `launch.sh`)

When the targets don't line up with a layer set — re-running just the failures from a prior run, mixing modules across layers, mixing per-target budgets — use [`launch.sh`](../launch.sh) instead of writing a new bash loop:

```bash
# Foreground, single target
./launch.sh --run-id rerun_001 --project /path/to/curve25519-dalek \
    --vstd-root /path/to/vstd src/edwards.rs

# Background (detached), mixed result-dirs and per-target budgets via file
cat > /tmp/targets <<EOF
results   | src/lemmas/field_lemmas/u64_5_as_nat_lemmas.rs | 60
results-C | src/edwards.rs                                  | 90
results-C | src/window.rs
EOF
./launch.sh --detach --run-id rerun_002 \
    --project /path/to/curve25519-dalek --vstd-root /path/to/vstd \
    --targets-file /tmp/targets
# → tail -f launcher_rerun_002.log | grep --line-buffered '^MARKER'
```

`launch.sh` is sequential by design (one project worktree → cargo-lock contention plus `failure_memory.json` read-modify-write races make parallel-on-one-project a footgun — see **Creating a clean admitted worktree** below for how to make the isolated checkouts each run needs). Each completed target emits one `MARKER` line, easy to grep/Monitor.

**Always pass `--detach` when launching from inside Claude Code's Bash tool.** The tool teardown does a `killpg` on its child process group, so plain `nohup … & disown` quietly dies between targets. `--detach` re-execs through Python's `start_new_session=True` (POSIX `setsid`), reparenting the orchestrator to launchd (`PPID=1`) where the tool can't reach it. Foreground (no `--detach`) is fine for interactive shells and short runs.

**Creating the admit() skeleton in place (`--admit`).** Targets normally arrive *already admitted* (e.g. checked out from an `eval/admitted-*` ref in the project worktree). To build that skeleton from proven source instead, pass `--admit` (opt-in): before each run, `launch.sh` runs [`admit.py`](../admit.py) on the target in place, admitting `proof fn` bodies + inline `proof { ... }` blocks while preserving `spec fn` definitions, exec code, and `axiom_*`. (`admit.py` is a top-level init/harness tool — a sibling of `run.py` — not an agent skill, since the proof agent never calls it during a round.) `--admit-mode` picks the pass: `auto` (default — `lemmas/` & `specs/` → `fn-bodies`, else → `proof-blocks`, mirroring inference-dalek's `construct_admitted_state`), `fn-bodies`, `proof-blocks`, or `both`. It runs before `start_admits` is counted, so the `MARKER` reflects the skeleton. **It is idempotent on already-admitted files but RESETS any proofs already present** — point it at a clean/ground-truth checkout, not work-in-progress.

```bash
# Admit the skeleton in place, then run (single target)
./launch.sh --run-id reset_001 --project /path/to/curve25519-dalek \
    --vstd-root /path/to/vstd --admit src/edwards.rs

# Or build the skeleton directly (standalone, debuggable like run.py / replay.py)
python admit.py /path/to/curve25519-dalek/src/edwards.rs --in-place --mode auto
```

**Resuming a sweep (`--skip-existing`).** To continue a sweep that was interrupted (e.g. by a rate-limit halt — see **Branch-local additions**) or partially failed, re-run the same command with `--skip-existing`: each target already recorded in `<results-dir>/proven_registry.json` (a genuine `success`) is skipped, and only not-yet-done targets run. The registry is at the results-dir level (shared across run-ids), so you can give the resume a **fresh `--run-id`** to keep clean per-target dirs and preserve the prior run's artifacts while still skipping the proven set. A target halted by a 429 is recorded `RATE_LIMITED` (not `COMPLETE`), so it stays out of the registry and re-runs. `run_proofonly_layers.sh` forwards `--skip-existing` too. Keep it one sweep per worktree — a resume *replaces* the original, it doesn't run alongside it.

### Creating a clean admitted worktree (the run's starting state)

A run wants the target in its **admitted starting state** — `proof fn` bodies
replaced by `admit()`, `spec fn` defs / exec code / `axiom_*` left intact —
inside an isolated checkout so the run never dirties your main tree. That
admitted state is exactly what inference-dalek's **`construct_admitted_state()`**
(in `inference_dalek/eval/starting_state.py`) builds: it admits proof bodies
under `lemmas/` + `specs/` and inline `proof { ... }` blocks in the exec files,
preserves `common_lemmas` + spec defs, validates with
`cargo verus verify -p curve25519-dalek`, and commits the result to the
**`eval/admitted-start`** ref (the "HAB eval: admit all proof bodies" commit).
The worktree half of that pipeline is its sibling `StartingStateManager.checkout()`
(a thin `git worktree add`). This repo's [`admit.py`](../admit.py) is the
**in-repo, single-file counterpart**: its `create_admit_worktree()` /
`admit.py --worktree` does the checkout, and its body pass (also exposed as
`launch.sh --admit`) is the per-file mirror of `construct_admitted_state`'s
admission.

The dalek-lite project is a Cargo **workspace**: the worktree is added at the git
**repo root** (`.../dalek-lite`, which holds the workspace `Cargo.toml`); the
`curve25519-dalek/` **member** subdir is what you pass as `--project` (the JSON
result surfaces it as `project`). Two ways to get the worktree, both verified
end-to-end:

**A — check out the pre-built admitted ref (no admit step needed):**

```bash
REPO=/path/to/dalek-lite                 # project git repo root (the Cargo workspace)
# create_admit_worktree: `git worktree add --detach` at the already-admitted ref.
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref eval/admitted-start
# → {"project": "/tmp/dalek-wt/curve25519-dalek", ...}; edwards.rs already = 92 admits.
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --remove    # when done
```

**B — build the skeleton from clean proven source (`--ref main` + `--admit-target`):**

```bash
REPO=/path/to/dalek-lite
# Checks out main detached (--detach is implicit — a non-detached add of `main`
# fails with "main is already used by worktree" since the primary checkout holds
# it), then admits each --admit-target in place (0 → 92 admits for edwards.rs).
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref main \
    --admit-target curve25519-dalek/src/edwards.rs
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --remove
```

`create_admit_worktree` is just `git worktree add --detach <dest> <ref>` (plus the
optional in-place body pass), so the equivalent by hand is
`git -C "$REPO" worktree add --detach /tmp/dalek-wt <ref>` then `launch.sh --admit`
/ `python admit.py <file> --in-place`. The body pass **resets any proofs already
present**, so it must run on a clean checkout — a fresh worktree is exactly that.
The two paths are interchangeable: A reuses the committed `construct_admitted_state`
output; B reconstructs the body pass locally.

**Warm vstd once on a brand-new worktree.** The first module-scoped `verus_check`
on a fresh worktree spuriously fails (`--verify-module` leaks into an uncompiled
vstd → "could not find module"). Warm the build target once before the first run
— `cargo verus verify -p curve25519-dalek` from the member dir (~40s, no module
filter) — and subsequent module-scoped checks resolve. (Not a `-p` bug; the
no-`-p` form fails identically.)

> **Running several at once.** Because each worktree is a fully isolated
> checkout, you *can* fan out parallel runs — but give each its own worktree
> **and** its own `--results` dir. Fresh `git worktree` creation mutates shared
> repo metadata, so serialize it (the peel launcher does this with a repo-scoped
> lock); after launch, separate worktrees clear cargo's `.cargo-lock` contention.
> The separate `--results` clears the `failure_memory.json` /
> `proven_registry.json` / `catalog_cache.json` read-modify-write race (those
> are keyed off the results root, not the project). One launcher per worktree.
> Sharing either the worktree or results root reintroduces a race.

### Inspecting results

```bash
python replay.py results/<run_id>/<target_id>/claude_raw/round_1.jsonl   # pretty-print stream-json
python replay.py <jsonl> --only tool_use                                 # filter event class
python replay.py <jsonl> --index                                         # event-count summary
python replay.py <jsonl> --full                                          # no truncation
tail -f results/<run_id>/<target_id>/cli.log                             # live skill-call log
```

When a run produces unexpected results (fake-greens, premature COMPLETE,
rlimit / verus-timeout failures, etc.), see
[docs/diagnostics.md](docs/diagnostics.md) — playbook of recurring
failure patterns with `jq`/`grep` detection commands and root-cause notes.

### Inspecting failure memory

```bash
python lib/failure_memory.py ./results --function <fn_name>
python lib/failure_memory.py ./results --function <fn_name> --as-prompt   # render as prompt block
```

### Running individual skills standalone

Each skill under `skills/` is a self-contained CLI that prints JSON to stdout and supports `-h`. Useful for debugging what the agent will see:

```bash
python skills/verus_check.py <file.rs> --project <root>
python skills/spec_check.py snapshot <file.rs> --out snap.json
python skills/spec_check.py verify   <file.rs> --against snap.json
python skills/search_semantic.py "pow2 adds and multiplies" --project <root> --catalog-cache <cache>
python skills/search_module.py "vstd::arithmetic::mul" --project <root> --catalog-cache <cache>
python skills/search_macro.py --name-prefix lemma_u8_pow2 --project <root> --catalog-cache <cache>
python skills/search_proven.py --results <results_root> --name lemma_foo
```

No linter, no build step, no third-party deps. The codebase is plain Python 3.11+ with no `pyproject.toml`; runtime correctness is observed via real runs against the dalek-lite project. The one exception is the `tests/` suite — small stdlib `unittest` tables pinning the subtle admit-counting / decision logic in `run.py` (`test_admits.py`), the spec-integrity gate (`test_spec_check.py`), and the peel builder (`test_peel.py`). Run the whole suite with `python3 -m unittest discover -s tests`, or a single module with e.g. `python3 -m unittest tests.test_admits`.

### One-time skill discovery setup

Claude Code auto-discovers skills under `.claude/skills/`. Symlink the project's `skills/` there once:

```bash
mkdir -p .claude/skills && ln -sfn "$(pwd)/skills" ".claude/skills/dalek-lite-mvp"
```

`.claude/` is in `.gitignore` — this is local-only setup.

## Architecture

### One driver, one loop

`run.py` is the entire orchestrator (~6.2k LOC on `spec_gen`; the MVP baseline was ~550). It:
1. Snapshots function signatures (`spec_check.py snapshot`).
2. Renders `prompt.md` with target/project/module/snapshot/cache/failure-memory placeholders, writes `prompt_rendered.md` for reproducibility.
3. Loops up to N rounds. Round 1 invokes `claude -p --session-id <uuid> --verbose --output-format stream-json`; later rounds invoke `claude --resume <session_id> -p` with structured round-history feedback as the continue message. The session is pinned by **explicit UUID**, not `-c`'s mtime-based "most recent session in this dir" lookup — `-c` is globally scoped to the OAuth user, so a concurrent interactive Claude Code session in the same project dir would silently hijack the harness's continuation rounds (`run_claude_round`, `run.py:3759-3770`).
4. After each round: spec-drift gate → verus check → record `round_N.json`.
5. Decides whether to continue: an agent `COMPLETE` claim that verus corroborates (zero hard `admit()`, no integrity drift) breaks early; a cheat/integrity drift (`SPEC_DRIFT`, …) breaks; otherwise continue.
6. On exit: the pure `run._final_end_reason` gate records the authoritative end_reason. **`COMPLETE` is recorded only when Verus is okay for the configured gate scope, no integrity gate drifted, AND zero hard `admit()` remain in scope** — `admit()` makes Verus accept any postcondition trivially, so verus_okay alone is insufficient. The gate can **promote** an over-cautious `LIMIT`/empty claim to `COMPLETE` when the worktree is truly green (it does *not* require the agent to have claimed done); infrastructure + cheat labels (`RATE_LIMITED`/`RETRY_EXHAUSTED`/`USER_INTERRUPTED`/`TRANSPORT_ERROR`, `SPEC_DRIFT`/`AXIOM_DRIFT`/`TOOLING_DRIFT`/`FORBIDDEN_CONSTRUCT`/…) stay explicit and non-promotable.

There is no orchestration class hierarchy. No `VerusAgent`, no `RepairLoop`, no `RecoveryCascade`. If a round-handling question arises there is exactly one place to look.

### Process-group lifecycle (important)

`claude` is spawned with `start_new_session=True` so all descendants live in one process group. `run.py` installs a SIGTERM/SIGINT/SIGHUP handler that `killpg`s that group, and post-completion always `killpg`s again. Without this, killing `run.py` orphans claude plus its async subprocesses (cargo verus, z3, Monitor poll loops) — they will run forever. Preserve this behaviour when editing the subprocess management code.

### Skills as CLIs (not Python imports)

`skills/*.py` are invoked by the agent via Bash, not imported by `run.py`. Contract:
- print JSON on stdout
- log human-readable trace to `$CLI_LOG_PATH` (set by `run.py` to per-task `cli.log`)
- exit code mirrors what's in JSON (`okay: false` → non-zero)

Adding a new skill = drop a new file matching this shape, mention it in `prompt.md` and `skills/SKILL.md`. That is the entire extension protocol — no schema negotiation, no registration step.

### Shared catalog

The four search skills (`search_semantic`, `search_module`, `search_macro`, `search_proven`) share `lib/catalog.py`, which builds a single canonical symbol catalog from project source AND optionally vstd. The catalog is cached at `<results_root>/catalog_cache.json` and reused across skill invocations within a run — **the prompt tells the agent not to rebuild it**. When extending search skills, prefer reading the same cache rather than re-walking the source tree.

### Spec integrity gate

`spec_check.py` snapshots every `fn` header + `requires` + `ensures` + `decreases` + `#[verifier::external_body]` attribute before the run, and verifies the snapshot after each round. **Any spec drift = the round fails and the loop breaks** (`end_reason: SPEC_DRIFT`). This exists because the agent's incentive is to make verus pass — weakening specs is the cheapest way. Don't relax this gate.

The prompt also explicitly forbids `#[verifier::external_body]` (silently bypasses SMT), `assume(...)`, and introducing new `admit()` calls.

Three sibling integrity gates in `run.py` guard the same "agent's incentive is to fake a green" threat and are checked the same way (snapshot a baseline before the loop, diff after each round, break + a non-promotable `end_reason` on drift — folded into `_final_end_reason` so even a budget-bail exit can't be promoted to COMPLETE):
- **Axiom integrity** (`end_reason: AXIOM_DRIFT`): the COMPLETE counter excludes `admit()` inside `proof fn axiom_*` bodies, so a *new* `axiom_*` is a fake-green vector. `lib/admits.py::axiom_fn_names` snapshots the axiom-name set across target + siblings + allow-edit deps; any name not in the baseline fails the round.
- **Tooling integrity** (`end_reason: TOOLING_DRIFT`): the harness's own verification skills are re-read from disk every round (`verus_check.py` / `spec_check.py` run as subprocesses; the agent runs the rest via Bash), and the proof agent shares this repo as its cwd with `Edit/Write/Bash` under `bypassPermissions` — so it *can* rewrite a skill to always return `okay=true`. `run.py` snapshots a SHA-256 of every `*.py` under `skills/` + `lib/` before the loop and diffs after each round; any add/edit/delete fails the round (checked **first** in the decision block, since a doctored tool makes the other gates' results untrustworthy). This is detection, not prevention — the same model as the spec/axiom gates — because tool-scoping can't close it: `Bash` is a write primitive and `--allowedTools` is a no-op under `bypassPermissions`. Don't relax these gates.
- **Forbidden-construct integrity** (`end_reason: FORBIDDEN_CONSTRUCT`): `assume(...)` and `#[verifier::external_body]` each discharge a proof obligation *without* an SMT proof (`assume(false)` closes any goal; `external_body` skips the body), and neither leaves an `admit()` or a new `axiom_*` for the other counters to catch — so a new `lemma_*` with `assume(false)`, or a new `external_body` helper, is a fake-green vector. `lib/admits.py::count_forbidden_constructs` (comment/string-aware) snapshots a baseline count across target + siblings + allow-edit deps; any *increase* fails the round. This is the only one of the three that runs even when the spec gate is off (experiment mode), so it's also what catches a new `external_body` helper there — the spec gate's own new-fn `external_body` check (`spec_check.py`) covers the normal path.

### State on disk, not in Python

Everything inspectable lives as JSON under `results/`:
- `results/<run_id>/<target_id>/{result.json, round_N.json, prompt_rendered.md, spec_snapshot.json, cli.log, claude_raw/round_N.jsonl, claude_memory/}` — per task (`claude_memory/round_N/` snapshots Claude Code auto-memory; `latest/` is carried into fresh auto-reset sessions)
- `results/<run_id>/layer_summary.json` — when run_layer is used
- `results/failure_memory.json` — cumulative; per-`(module, function)` failure records, injected into the prompt on retry (most recent 3 attempts via `as_prompt_block`)
- `results/proven_registry.json` — cumulative successes; consulted by `search_proven.py` and by `run_layer.py --skip-existing`
- `results/catalog_cache.json` — symbol catalog cache shared by search skills

`jq` and `less` are the dashboard.

### Prompt is data

`prompt.md` is the single source of truth for the agent's rules and workflow. `run.py` reads it fresh each round and substitutes `{TARGET_PATH}`, `{PROJECT_ROOT}`, `{MODULE_PATH}`, `{SPEC_SNAPSHOT}`, `{CATALOG_CACHE}`, `{RESULTS_ROOT}`, `{VSTD_FLAG}`, `{FAILURE_MEMORY_BLOCK}`, plus the **scope placeholders** `{TASK_SCOPE_INTRO}`, `{EDIT_SCOPE_RULE}`, `{WORKFLOW_SCOPE_STEPS}`, `{ADMIT_SCOPE_GUIDANCE}`, `{SESSION_END_CHECKS}` (filled at `run.py:669-673`). Editing the prompt is a first-class way to change agent behaviour — no code change required.

**Scope-aware prompt (commit `7715f11`).** The scope placeholders make one prompt serve both the default single-target task and the whole-crate experiment modes (`run._WHOLE_CRATE_MODES`). In single-target mode they render "work/edit/read only the target file"; in whole-crate mode they render "the editable list is the COMPLETE source of truth — do not infer frozen scope from the floor name" and the admit inventory spans the full editable list, not just the target. This killed a false `NEEDS_DECOMP` escalation where field-floor "frozen specs" wording made the agent declare a reachable repair out of scope.

### Non-goals (don't add these without checking the spec)

- No batch runner inside `run.py` (one target per invocation; script with bash).
- No cascade levels / widening / decomposition / escalation across rounds.
- No subagent orchestration by the harness (the model can use Task itself; the harness doesn't coordinate).
- No cross-module campaign state beyond `failure_memory.json` and `proven_registry.json`.
- No web UI / dashboard.

Each of these is a deferred extension in `docs/extension_spec.md` with a documented trigger. If you find yourself wanting one, check whether the trigger has actually fired before building it.

**Note:** the `spec_gen` branch has crossed these non-goals on purpose (session auto-reset is a form of cross-round recovery; experiment-mode is a new run-mode axis; NEEDS_DECOMP is a cross-round escalation). They are recorded in **Branch-local additions** below and in `docs/extension_spec.md`, so the drift is a documented choice rather than a silent one. The non-goals list above still describes the MVP / merge-to-`main` target shape.

## Branch-local additions (`spec_gen`, not the MVP merge target)

The architecture described above — one loop, a ~550-LOC `run.py`, the Non-goals list — is the shape `main` should converge back to. The `spec_gen` research branch deliberately adds four things on top of it. They are documented here so the drift is recorded, not silent; none are load-bearing for the MVP, and a merge to `main` should re-justify or drop each.

- **Experiment-mode** (`--experiment-mode spec-proof|proof-only|contract-only|bridge-specs|bridge-full|field-floor`, requires `--experiment-allow-edit`). **Shared vocabulary (peel):** canonically, each mode below is one point on a single totally-ordered **peel-depth** axis over the proof *stack* — **P1 proofs → P2 lemmas → P3 specs → P4 contract**, strictly cumulative (depth N peels the top N layers of the stack; the trusted floor — exec code + every `axiom_*`/`assume`/`external_body` — is never peeled, by construction). The `--experiment-mode` names are the run-side labels `peel.py` echoes for particular cuts; the canonical axis and the "cone breadth, not peel depth" argument live in the CryptoProver paper and `peel_manifests/`. A *new task axis* not in the original `extension_spec.md`: instead of only filling `admit()`s under fixed specs, the agent reconstructs stripped fn-header specs (`spec-proof`), only adds proof scaffolding to seeded dep bodies (`proof-only`), rebuilds the anchor's stripped proof body + deleted helper lemmas under a frozen contract (`contract-only`), reconstructs deleted shared `open spec fn`s pinned by frozen consumers (`bridge-specs`), or reconstructs the whole decompress proof tree cut below the curve layer (the decompress lemmas + the decompress-path curve lemmas, across 2 editable lemma files) as a **pure proof-reconstruction** task with EVERY spec definition frozen — including the map — so the user-facing contract is structurally un-weakenable (`bridge-full`). The generated field-floor cut (`field-floor`) uses the same whole-crate gate vocabulary as the bridge rungs but a broader generated editable floor. Injected into the prompt via `build_experiment_block`. Whole-crate modes (`field-floor` and `run._BRIDGE_MODES`) add a whole-crate verify + frozen-file guard (`FROZEN_EDIT`); for `bridge-full` all 10 decompress-path lemmas are deleted outright (`--delete-fn`) and the agent re-derives each (signature + contract + proof) — safe because the API ensures reference only frozen spec fns and the editable files hold zero spec fns, so no re-derived contract can weaken the guarantee. An explicit deterministic name list in `demo_decompress.sh`. (`bridge-specs` stays the dedicated map-reconstruction rung — it reconstructs the map, pinned by the frozen `to_edwards` proof.) The modes map to `demo_decompress.sh` rungs (`--formal-spec`/`--no-spec`/`--no-lemmas`/`--no-anchor-proof`/`--no-bridge-specs`/`--no-bridge-lemmas`; see `docs/website_backend.md`); `field-floor` is generated from peel manifests rather than a demo_decompress rung. See **E6** in `docs/extension_spec.md`. **Build-side reconciliation (canonical):** the init-states for these modes are now expressed as **peel manifests** (`peel_manifests/*.json`) and built by `peel.py` — one *peel-depth* axis (P1 proofs → P2 lemmas → P3 specs → P4 contract) with a `proof_op` per file and an enforced **pin rule** (P4 / spec-delete cuts refuse to build without `--pin`). `peel_run.sh` is the thin bridge: it builds the peeled worktree, reads `experiment_mode` + `editable_files` out of peel's JSON, and invokes `run.py` with the matching `--experiment-mode`/`--experiment-allow-edit`. There is deliberately **no depth→mode inference** — the mode is declared in the manifest (depth cannot express "strip stratum k, keep 1..k-1 as the pin"). The five frozen-contract decompress rungs have ready-made manifests; the gate-OFF `spec-proof` rungs (`--no-spec`/`--no-lemmas`) and `field-floor` / `--strip-to-fields` (via `peel.py --classify`) are covered in `peel_manifests/README.md` + `docs/spec_gen_runbook.md` §1.
- **Session auto-reset** (`--auto-reset`, on by default; `--max-auto-resets`, `--stall-max-duration-sec`, `--bloat-threshold-tokens`). In-loop recovery: when two consecutive rounds make no admit progress in under N minutes (*stall*), or the session's cache-creation tokens exceed a threshold (*bloat*), the next round starts a fresh `claude` session instead of `-c` continuation. This crosses the "no escalation across rounds" non-goal; it is a lightweight take on **E2**'s context-explosion pain (reset the one session to shed context, rather than E2's prescribed subagents). Tracked via `reset_round_starts`.
- **NEEDS_DECOMP escalation** (`END_REASON:NEEDS_DECOMP`). A *cross-round escalation*: instead of grinding to the time limit, the agent can declare a proof blocked on **missing infrastructure** (a helper lemma/chain that doesn't exist, or a sub-lemma split) and name what's missing. The loop breaks on it, the label survives into `result.json` / `failure_memory` / `layer_summary`, and a fresh `run_task` on the same target detects the record and retries with +2 rounds, 1.5× wall-clock, and a "build the named infrastructure first" directive. This crosses the "no cascade / escalation across rounds" non-goal; it is a lightweight take on **E1**'s multi-level cascade (a single LIMIT/NEEDS_DECOMP/COMPLETE label + budget bump, not E1's L0→L3 level machine). The final-state gate is the pure, unit-tested `run._final_end_reason`; the parse is `run.END_REASON_RE` (both pinned in `tests/test_admits.py`). See **E1** status note in `docs/extension_spec.md`.
- **Spec-inference skill** (`skills/infer_verus_spec/`, doc-only) plus `strip_specs.py` — a **top-level init/harness tool** (sibling of `run.py` / `admit.py`, NOT an agent skill) that builds the stripped/starting eval inputs. The proof agent never calls it during a round; it is part of init-state construction, same as `admit.py`.
- **Peel — the unified init-state builder** (`peel.py`, `peel_run.sh`, `peel_manifests/`; `tests/test_peel.py`). Also a **top-level init/harness tool**, NOT an agent skill. It is the *reconciliation* of the experiment-mode rungs into one data-driven path: `strip_specs.py` (delete/strip verbs) + `admit.py` (admit verb) become one **peel-depth** axis (P1 proofs → P2 lemmas → P3 specs → P4 contract, cumulative + totally ordered), driven by a JSON **manifest** (per-file `proof_op`/`lemmas`/`spec_fns`/`contract_fns`; every listed file is editable, the rest are the frozen guard input). It composes — does not reimplement — the existing transforms (`lib.admits` admit; `strip_specs.delete_text`/`strip_text`/`strip_proof_from_fns`) and reuses `admit.create_admit_worktree` for the git half. The **pin rule** (`peel._require_pin`) refuses any P4 contract-strip or P3 spec-delete without a declared pin (`proof`/`consumer:NAME`/`oracle:REF`), the same soundness discipline the curated rungs got from freezing. `peel.py --classify` generates the directory-cut (`--strip-to-fields`) manifest; `peel.py --surface` previews a cut without touching files. `peel_run.sh` chains `peel.py --worktree` → `run.py` (the only peel-side wiring; the peel *builder* leaves `run.py` untouched). The one `run.py` change in this work is separate — the **spec-definition gate**: `run.py` now passes `--check-spec-defs` to `spec_check verify` whenever the spec gate is on, freezing existing `spec fn` *bodies* (not just headers) so a definition can't be redefined to hollow a frozen contract (`skills/spec_check.py`; tests in `tests/test_spec_check.py`). Runbook: `docs/spec_gen_runbook.md` §1; mapping table: `peel_manifests/README.md`.
- **Rate-limit halt + resume** (`END_REASON:RATE_LIMITED`; `launch.sh --skip-existing`). *Operational hardening, not an `extension_spec.md` E-feature.* When `claude -p` returns HTTP 429 (5-hour session/quota limit, overage disabled), `run_claude_round`'s result carries `is_error` + `api_error_status == 429`. The round loop catches this **before the verus gate** and sets `end_reason = "RATE_LIMITED"`, so a trivial (zero-hard-admit) target can't be promoted to a false `COMPLETE` off a round the agent never ran. `RATE_LIMITED` is preserved as the **top priority** in the pure `run._final_end_reason` (above the `done_for_real → COMPLETE` promotion — see the gate's docstring and the `FinalEndReasonGate` tests pinning it), `main()` returns **exit code 42**, and `launch.sh`/`run_proofonly_layers.sh` **break the whole sweep** on rc 42 (every later target would just be rejected too until the window resets). Distinct from the existing heuristic `RATE_LIMIT_OR_HANG` guard, which needs `duration > 300` and so never catches an *instant* 429 rejection. To resume after the window reopens, re-run the same command with **`--skip-existing`** (added to `launch.sh`, mirroring `run_layer.py`): it skips targets already in `<results>/proven_registry.json` — which is at the results-dir level, shared across run-ids, so a fresh `--run-id` resumes correctly *and* preserves the prior run's artifacts. A 429-halted target is recorded `RATE_LIMITED` (never `COMPLETE`), so it stays out of the registry and re-runs. **Footgun (observed):** two `launch.sh` sweeps on one project worktree race on the cargo lock + `failure_memory.json`/`proven_registry.json` — keep it one sweep per worktree (the `--skip-existing` resume replaces, not parallels, the original).
- **Transport / retry-exhaustion / user-interrupt classification** (`END_REASON:RETRY_EXHAUSTED`, `END_REASON:USER_INTERRUPTED`, `END_REASON:TRANSPORT_ERROR`; commit `7715f11`, user-interrupt split added later). *Operational hardening, sibling to the rate-limit halt above.* A `claude -p` round can exit **nonzero with no `result` line** — the SDK gave up after exhausting its API retries, the user/operator interrupted tool use, or the transport died — distinct from an instant 429. `run._classify_claude_no_result_exit` inspects the round: a positive exit whose last stream event is a `system`/`api_retry` becomes `RETRY_EXHAUSTED`; a raw stream containing `[Request interrupted by user]` / `[Request interrupted by user for tool use]` becomes `USER_INTERRUPTED`; any other positive no-result exit becomes `TRANSPORT_ERROR`; negative signal kills (e.g. `-9`) are left to the existing timeout/deadline path. The round loop classifies this **before the spec/verus gates** and writes a visible `round_N.json`, so a zero-hard-admit target can't be promoted to a false `COMPLETE` off a round the agent never actually ran. These labels are preserved alongside `RATE_LIMITED` at the **top** of the pure `run._final_end_reason`, above the `done_for_real → COMPLETE` promotion, and are pinned by the `ClaudeNoResultExitClassification` tests in `tests/test_admits.py`.
- **Whole-crate verification timeout** (commits `b3f3c4f`/`d0ad4c8`). The bridge / whole-crate experiment modes verify the entire package (`cargo verus verify -p curve25519-dalek`, no `--verify-module`), which is far slower than a single module. `skills/verus_check.py` defaults the timeout to **900s for `--whole-crate`** vs **300s for module checks** (`verus_check.py:302-304`), and the harness's own whole-crate checks budget above the 300s module wall to match. A timed-out whole-crate check is held as *indeterminate* (not a scored plateau) so a slow verify can't masquerade as proof failure.
- **Subagent delegation hint** (`spec_gen_subagent_context` sub-branch; `prompt.md` section "Delegate hard sub-proofs to a subagent"). A *prompt-only* lever against context degradation (the dominant observed failure mode): the agent is told to push read-heavy exploration (lemma search, multi-file reads, Verus/Z3 error digestion) into the built-in **`Agent`** subagent tool, which returns only a short summary. **Findings (2026-06-01), see the E2 note in `docs/extension_spec.md`:** (1) the subagent tool emits as `Agent` in the headless `claude -p` stream (the earlier "Task" wording is tolerated but `Agent` is the literal name); (2) prompt-only encouragement **never induced delegation** (0 spawns across 2 runs / 7 rounds) — the tool name was never the blocker, since the agent grinds inline regardless; (3) **subagents already isolate context** (a delegation returned ~1.7 KB into the parent) — the bloat seen in a `--mode coordinator` prototype came from the *coordinator reading `.rs` files itself*, not from subagents, so that coordinator + a `prove_obligation` skill were **built then removed as redundant**. **Kept:** only `--bloat-threshold-tokens` 300000 → 200000 (validated). The standing direction is **read-only offload + compaction** (delegate heavy reads to the already-isolating `Agent` tool; lean on the discovery-brief + auto-reset as the compaction layer), since for write-heavy proof work single-threaded + compaction beats a writing-coordinator. The open gap is *behavioral*: nothing short of separate `run.py`-level prover processes can force the driver to delegate — `--disallowedTools` propagates to subagents and is `Bash cat`-bypassable.

## Repo conventions

- Python 3.11+, stdlib only. Don't introduce dependencies without a clear reason.
- If a fix requires changes in more than two files, that's a sign of design drift — pause and reconsider rather than spreading the change.
- Skills must be debuggable standalone (`python skills/foo.py ...` at the shell should produce the same JSON the agent sees).
- The dalek-lite project Verus targets typically live under `/path/to/dalek-lite/curve25519-dalek/src/` in the dev_log/README; treat those paths as examples — don't hardcode them in code.
