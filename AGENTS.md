# AGENTS.md

This file provides guidance to coding agents working in this repository. It mirrors `CLAUDE.md` in a shorter form; keep the two in sync. Note: references to the *spawned proof agent* below name `claude` / Claude Code deliberately — `run.py` shells out to `claude -p` regardless of which agent is reading this file, so those are facts about the harness, not the reader.

## What this repo is

A slim Verus proof-synthesis agent for the dalek-lite (curve25519-dalek) Rust codebase. The agent's job is to replace `admit()` calls in Verus-annotated Rust files with real proofs that pass `cargo verus`. The MVP target was ~1k LOC of Python, small enough to read in one sitting; the `spec_gen` research branch has grown far past that (currently ~10k LOC across `run.py` + `skills/` + `lib/`) — see CLAUDE.md's **Branch-local additions** for what was added on top of the MVP and why.

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

When `--max-task-minutes` is omitted the budget auto-scales: `max(20, 1.5 * num_admits)` minutes. SIGKILL fires on the entire `claude` process group at the deadline — `claude` spawns descendants (cargo verus, z3, Monitor poll loops) that won't die otherwise.

### Running a layer set (multiple targets sequentially)

```bash
python run_layer.py A --project /path/to/curve25519-dalek --rounds 5
python run_layer.py A --project ... --run-id layerA_001 --skip-existing
```

Layer Sets A/B/C/D are all wired in (mirrored from `inference-dalek/inference_dalek/eval/domain_layers.py`): A = field repr + reduce (9 modules), B = serialize (6), C = edwards base + ops (15), D = ristretto (5). `run_layer.py` is sequential — for parallelism, fan out `run.py` invocations with `xargs -P` (each writes to its own per-task dir).

### Running an arbitrary list of targets (`launch.sh`)

When the targets don't line up with a layer set (re-running prior failures, mixing modules across layers, per-target budgets), use `launch.sh` instead of a hand-rolled bash loop. It accepts positional `.rs` paths or a `--targets-file` (`<results-dir>|<rel-path>[|<budget-min>]` per line), and emits one `MARKER` line per completed target.

```bash
# Foreground, single target
./launch.sh --run-id rerun_001 --project /path/to/curve25519-dalek \
    --vstd-root /path/to/vstd src/edwards.rs

# Background (detached), mixed result-dirs / budgets via file
cat > /tmp/targets <<EOF
results   | src/lemmas/field_lemmas/u64_5_as_nat_lemmas.rs | 60
results-C | src/edwards.rs                                  | 90
EOF
./launch.sh --detach --run-id rerun_002 --project /path/to/curve25519-dalek \
    --vstd-root /path/to/vstd --targets-file /tmp/targets
# watch: tail -f launcher_rerun_002.log | grep --line-buffered '^MARKER'
```

Pass `--detach` whenever launching from an agent's Bash tool that `killpg`s its child process group (plain `nohup … & disown` dies between targets); `--detach` re-execs through Python's `start_new_session=True` (POSIX `setsid`), reparenting the orchestrator to PID 1. `--admit` builds the `admit()` skeleton in place before each run (resets existing proofs — point it at a clean checkout); `--admit-mode` is `auto|fn-bodies|proof-blocks|both`.

### Creating a clean admitted worktree (the run's starting state)

A run wants the target in its **admitted starting state** (`proof fn` bodies → `admit()`; `spec fn` defs / exec / `axiom_*` intact) inside an isolated checkout. That state is what inference-dalek's **`construct_admitted_state()`** (`inference_dalek/eval/starting_state.py`) builds and commits to the **`eval/admitted-start`** ref; `StartingStateManager.checkout()` is its `git worktree add` half. This repo's `admit.py` is the in-repo, single-file counterpart: **`create_admit_worktree()` / `admit.py --worktree`** does the checkout, and its body pass (also `launch.sh --admit`) mirrors the admission.

The project is a Cargo **workspace** (repo root = `.../dalek-lite`, holds the workspace `Cargo.toml`; member package = `curve25519-dalek/`, the `--project`). The worktree is added at the repo root; the JSON result surfaces the member subdir as `project`. Two verified ways:

```bash
REPO=/path/to/dalek-lite                 # project git repo root (the Cargo workspace)

# A — pre-built admitted ref (skeleton already committed; no admit step):
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref eval/admitted-start
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs --project /tmp/dalek-wt/curve25519-dalek

# B — build skeleton from clean source. --detach is implicit, so this works even
#     though the primary checkout holds `main`; --admit-target admits in place:
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref main \
    --admit-target curve25519-dalek/src/edwards.rs
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs --project /tmp/dalek-wt/curve25519-dalek

python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --remove   # cleanup when done
```

`create_admit_worktree` is `git worktree add --detach <dest> <ref>` plus the optional body pass; by hand that's `git -C "$REPO" worktree add --detach /tmp/dalek-wt <ref>` then `launch.sh --admit` / `python admit.py <file> --in-place`. The body pass **resets any proofs already present**, so run it on a clean checkout (a fresh worktree is exactly that).

**Running several at once:** each worktree is isolated, so fan out parallel runs by giving each its own worktree **and** its own `--results` dir, then one `launch.sh` per worktree. The worktree clears cargo's `.cargo-lock`; the separate `--results` clears the `failure_memory.json` / `proven_registry.json` / `catalog_cache.json` race (keyed off the results root, not the project). Sharing either reintroduces a race.

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
2. Renders `prompt.md` with target/project/module/snapshot/cache/failure-memory + scope placeholders, writes `prompt_rendered.md` for reproducibility.
3. Loops up to N rounds. Round 1 invokes `claude -p --session-id <uuid> --verbose --output-format stream-json`; later rounds invoke `claude --resume <session_id> -p` with structured round-history feedback as the continue message. The session is pinned by **explicit UUID**, not `-c`'s mtime-based lookup, which a concurrent interactive Claude Code session in the same dir would silently hijack (`run_claude_round`, `run.py:3759-3770`).
4. After each round: spec-drift gate → verus check → record `round_N.json`.
5. Decides whether to continue: an agent `COMPLETE` claim that verus corroborates (zero hard `admit()`, no integrity drift) breaks early; a cheat/integrity drift (`SPEC_DRIFT`, …) breaks; otherwise continue.
6. On exit: the pure `run._final_end_reason` gate records the authoritative end_reason. **`COMPLETE` is recorded only when Verus is okay for the configured gate scope, no integrity gate drifted, AND zero hard `admit()` remain in scope** — `admit()` makes Verus accept any postcondition trivially, so verus_okay alone is insufficient. The gate can **promote** an over-cautious `LIMIT`/empty claim to `COMPLETE` when truly green (it does *not* require the agent to have claimed done); infrastructure + cheat labels (`RATE_LIMITED`/`RETRY_EXHAUSTED`/`USER_INTERRUPTED`/`TRANSPORT_ERROR`, `SPEC_DRIFT`/`AXIOM_DRIFT`/…) stay non-promotable.

There is no orchestration class hierarchy. No `VerusAgent`, no `RepairLoop`, no `RecoveryCascade`. If a round-handling question arises there is exactly one place to look.

### Process-group lifecycle (important)

`claude` is spawned with `start_new_session=True` so all descendants live in one process group. `run.py` installs a SIGTERM/SIGINT/SIGHUP handler that `killpg`s that group, and post-completion always `killpg`s again. Without this, killing `run.py` orphans `claude` plus its async subprocesses (cargo verus, z3, Monitor poll loops) — they will run forever. Preserve this behaviour when editing the subprocess management code.

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

**Sibling integrity gates.** Four more gates guard the same "the agent's incentive is to fake a green" threat, each snapshotted before the loop and diffed after every round (drift → break + a non-promotable `end_reason`): **axiom** (`AXIOM_DRIFT` — new `axiom_*`), **tooling** (`TOOLING_DRIFT` — any edit to `skills/`+`lib/` `*.py`; checked first, since a doctored tool makes the others untrustworthy), **forbidden-construct** (`FORBIDDEN_CONSTRUCT` — any *increase* in `assume(...)`/`external_body`, runs even when the spec gate is off), and the **spec-definition freeze** (`--check-spec-defs`, on whenever the spec gate is on — freezes `spec fn` *bodies*, not just headers, so a definition can't be redefined to hollow a frozen contract). See CLAUDE.md → **Spec integrity gate** for the full text. Don't relax these.

### State on disk, not in Python

Everything inspectable lives as JSON under `results/`:
- `results/<run_id>/<target_id>/{result.json, round_N.json, prompt_rendered.md, spec_snapshot.json, cli.log, claude_raw/round_N.jsonl, claude_memory/}` — per task (`claude_memory/round_N/` snapshots Claude Code auto-memory; `latest/` is carried into fresh auto-reset sessions)
- `results/<run_id>/layer_summary.json` — when run_layer is used
- `results/failure_memory.json` — cumulative; per-`(module, function)` failure records, injected into the prompt on retry (most recent 3 attempts via `as_prompt_block`)
- `results/proven_registry.json` — cumulative successes; consulted by `search_proven.py` and by `run_layer.py --skip-existing`
- `results/catalog_cache.json` — symbol catalog cache shared by search skills

`jq` and `less` are the dashboard.

### Prompt is data

`prompt.md` is the single source of truth for the agent's rules and workflow. `run.py` reads it fresh each round and substitutes `{TARGET_PATH}`, `{PROJECT_ROOT}`, `{MODULE_PATH}`, `{SPEC_SNAPSHOT}`, `{CATALOG_CACHE}`, `{RESULTS_ROOT}`, `{VSTD_FLAG}`, `{FAILURE_MEMORY_BLOCK}`, plus the **scope placeholders** `{TASK_SCOPE_INTRO}`/`{EDIT_SCOPE_RULE}`/`{WORKFLOW_SCOPE_STEPS}`/`{ADMIT_SCOPE_GUIDANCE}`/`{SESSION_END_CHECKS}` that make one prompt serve both the single-target task and the whole-crate experiment modes (commit `7715f11`). Editing the prompt is a first-class way to change agent behaviour — no code change required.

### Non-goals (don't add these without checking the spec)

- No batch runner inside `run.py` (one target per invocation; script with bash).
- No cascade levels / widening / decomposition / escalation across rounds.
- No subagent orchestration by the harness (the model can use Task itself; the harness doesn't coordinate).
- No cross-module campaign state beyond `failure_memory.json` and `proven_registry.json`.
- No web UI / dashboard.

Each of these is a deferred extension in `docs/extension_spec.md` with a documented trigger. If you find yourself wanting one, check whether the trigger has actually fired before building it.

### Branch-local additions (`spec_gen`)

The `spec_gen` branch deliberately crosses several non-goals above; CLAUDE.md → **Branch-local additions** is the authoritative list. The pieces you are most likely to meet:
- **Experiment-mode** (`--experiment-mode spec-proof|proof-only|contract-only|bridge-specs|bridge-full|field-floor`, requires `--experiment-allow-edit`) and its build-side init-state builder **`peel.py`** + `peel_manifests/*.json` (one *peel-depth* axis P1 proofs → P4 contract, with a pin rule). `peel_run.sh` chains `peel.py --worktree` → `run.py`.
- **In-loop recovery / escalation** beyond the MVP loop: session **auto-reset** (`--auto-reset`, on stall/bloat) and **`NEEDS_DECOMP`** escalation (declare a proof blocked on missing infrastructure; a fresh retry gets +2 rounds / 1.5× wall-clock).
- **Run-mode `end_reason`s** the final-state gate (`run._final_end_reason`) preserves above any green: `RATE_LIMITED` (instant 429, exit 42, breaks the sweep), `RETRY_EXHAUSTED` / `USER_INTERRUPTED` / `TRANSPORT_ERROR` (nonzero/no-result `claude` exits, classified before the spec/verus gates), and `NEEDS_DECOMP`. Resume an interrupted sweep with `launch.sh --skip-existing`.
- **Whole-crate verification** for whole-crate modes (`field-floor` and `bridge-*`): `verus_check.py --whole-crate` verifies the whole package with a **900s** timeout (vs 300s for module checks).

## Repo conventions

- Python 3.11+, stdlib only. Don't introduce dependencies without a clear reason.
- If a fix requires changes in more than two files, that's a sign of design drift — pause and reconsider rather than spreading the change.
- Skills must be debuggable standalone (`python skills/foo.py ...` at the shell should produce the same JSON the agent sees).
- The dalek-lite project Verus targets typically live under `/path/to/dalek-lite/curve25519-dalek/src/` in the dev_log/README; treat those paths as examples — don't hardcode them in code.
