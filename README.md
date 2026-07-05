# CryptoProver

Agentic Verus proof- and spec-synthesis harness for the dalek-lite
(curve25519-dalek) Rust codebase — the artifact accompanying the
**CryptoProver** paper. One driver loop, CLI skills, integrity gates
(spec / axiom / tooling / forbidden-construct), and a data-driven **peel**
init-state builder that generates graded proof-reconstruction tasks up to
the full **field-floor** cut evaluated in the paper.

> This repository is a cleaned public snapshot of the research tree. Run
> artifacts and internal working notes are not included; the campaign statistics
> backing the paper live in `docs/run_stats/`.

## Headline result

From the canonical run record ([docs/run_stats/stage3_certificate_record.md](docs/run_stats/stage3_certificate_record.md)):

> Given only the user-facing API contracts, frozen specifications, a trusted
> arithmetic floor, and the measured pin set (124 frozen call sites + file
> skeleton + declared priors), a claude-fable-5 agent under this harness
> (`a56f284`) reconstructed the entire deleted proof cone of the dalek-lite
> **field-floor cut** — +11,024/−22,753 lines over the 26 editable files
> (48.5% of the ground truth's proof mass), 196 lemmas vs GT's 235 — to
> **whole-crate 0 errors** at default SMT limits, in 11.4 agent-hours across
> two pre-registered attempts, with zero integrity violations, verified
> independently on two machines (2,031 declarations verified).

The cut deletes all 22 lemma homes (235 lemmas: signature + contract + body)
and proof-strips all 4 API files (100 inline proof blocks) from a proven tip;
everything outside those 26 files is frozen. An earlier June-2026 campaign on
the same cut did not converge — that record ships too
([docs/run_stats/final_convergence_stats.md](docs/run_stats/final_convergence_stats.md))
as the failure baseline that motivated the staged methodology. The full
verification battery (independent two-machine re-verify, exec-immutability
audit, axiom/admit inventories, rlimit stratification) is in the record.

Start here:
- `docs/mvp_spec.md` — the core proof-agent design and rationale
- `docs/extension_spec.md` — deferred features and their triggers
- `docs/spec_gen_runbook.md` — running the peel / field-floor experiments
- `peel_manifests/README.md` — the peel-depth axis and manifest format
- `docs/run_stats/` — campaign records (certificate runs, convergence stats)

## Layout

```
cryptoprover/
├── run.py              # the driver — one target per invocation
├── run_layer.py        # iterate one of layer-sets A/B/C/D sequentially
├── launch.sh           # arbitrary target lists + Claude-Code-safe --detach
├── prompt.md           # the task prompt (template)
├── docs/
│   ├── mvp_spec.md     # what's in scope + design rationale
│   └── extension_spec.md  # the 5 deferred features, in detail
├── lib/                # support modules (not skills)
│   ├── catalog.py      # canonical symbol catalog (shared by search skills)
│   ├── failure_memory.py  # per-function persistent failure records
│   └── results.py      # result-dir helpers + dataclasses
└── skills/             # CLIs the agent invokes via Bash
    ├── SKILL.md
    ├── verus_check.py
    ├── spec_check.py
    ├── admit_inventory.py
    ├── search_semantic.py
    ├── search_module.py
    ├── search_macro.py
    ├── check_false_contract.py
    └── search_proven.py
```

## Prerequisites

- Python 3.11+
- Claude Code CLI (`claude`) on PATH, authenticated
- Verus / `cargo verus` installed and on PATH
- A Verus-annotated Rust project with at least one `admit()` to fill in

## One-time setup: make skills discoverable

Claude Code auto-discovers skills under `.claude/skills/`. Symlink this
project's `skills/` there:

```bash
cd /path/to/CryptoProver
mkdir -p .claude/skills
ln -sfn "$(pwd)/skills" ".claude/skills/dalek-lite-mvp"
```

Verify by running `claude` in this directory and typing `/skills`. You
should see `dalek-lite-mvp` listed. The skill keeps that historical name
because the harness targets dalek-lite.

## Running it

### Single module

```bash
# Basic: prove the admits in one file
python run.py /path/to/dalek-lite/curve25519-dalek/src/specs/field_specs.rs

# Budget + explicit run id + results dir
python run.py <target> --rounds 5 --run-id baseline_001 --results ./results

# Cheaper model for simpler modules
python run.py <target> --model sonnet    # haiku | sonnet | opus | claude-sonnet-4-6

# Include vstd in the catalog so skills find vstd lemmas
python run.py <target> --vstd-root /path/to/verus/vstd

# Emit diff.md showing admitted vs final vs ground-truth
python run.py <target> \
    --admitted-ref eval/admitted-layerA-debug \
    --truth-ref main

# Override project root (rarely needed; auto-detected from target)
python run.py <target> --project /path/to/cargo/root
```

The target file must live inside a buildable Cargo project (an ancestor
directory has `Cargo.toml`). `run.py` detects this automatically.

### Layer Set A (the EXP-013 benchmark target)

Layer Set A = **L0 + L1 = 9 modules** (4 field-repr modules + 5 field-reduce
modules). These are the modules inference-dalek verified 9/9 in EXP-013
at 950K tokens / 21:53 — use them as your reproduction benchmark.

```bash
# Sequential run across all 9 Layer Set A modules
python run_layer.py A \
    --project /path/to/dalek-lite/curve25519-dalek \
    --rounds 5 \
    --run-id layerA_001

# Resume after interruption (skips already-proven modules)
python run_layer.py A \
    --project /path/to/dalek-lite/curve25519-dalek \
    --run-id layerA_001 \
    --skip-existing
```

Summary emitted at `results/<run_id>/layer_summary.json`.

Layer sets A/B/C/D are all wired in. Module lists are mirrored from
`inference-dalek/inference_dalek/eval/domain_layers.py` —
A: field repr + reduce (9 modules), B: serialize (6),
C: edwards base + ops (15), D: ristretto (5). Pass the letter as the
positional arg: `python run_layer.py B ...`.

### Arbitrary target lists (use `launch.sh`)

When the targets you want don't line up with a layer set — e.g.
re-running just the failures from a prior run, mixing modules across
layers, or assigning per-target budget overrides — use
[`launch.sh`](launch.sh) instead of writing a new bash loop:

```bash
# Foreground, single target
./launch.sh --run-id rerun_001 --project /path/to/curve25519-dalek \
    --vstd-root /path/to/vstd src/edwards.rs

# Background (detached), mixed result-dirs and per-target budgets
cat > /tmp/targets <<'EOF'
results   | src/lemmas/field_lemmas/u64_5_as_nat_lemmas.rs | 60
results-C | src/edwards.rs                                  | 90
results-C | src/window.rs
EOF
./launch.sh --detach --run-id rerun_002 \
    --project /path/to/curve25519-dalek --vstd-root /path/to/vstd \
    --targets-file /tmp/targets

# Watch: each completed target emits one MARKER line
tail -f launcher_rerun_002.log | grep --line-buffered '^MARKER'
```

`launch.sh` is sequential by design — for one project worktree you do
not want parallel `run.py`s (cargo-lock contention plus
`failure_memory.json` read-modify-write races). See
[Creating a clean admitted worktree](#creating-a-clean-admitted-worktree)
below for how to make the isolated checkout each run starts from (and how
to fan out across several).

**`--detach` is required when launching from inside Claude Code's Bash
tool.** The tool teardown does a `killpg` on its child process group, so
plain `nohup … & disown` quietly dies between targets. `--detach`
re-execs through Python's `start_new_session=True` (POSIX `setsid`),
reparenting the orchestrator to launchd (`PPID=1`) where the tool can't
reach it. Foreground (no `--detach`) is fine for interactive shells and
short runs.

**Building the admit() skeleton (`--admit`).** Targets normally arrive
already admitted (e.g. checked out from an `eval/admitted-*` ref). To
create that skeleton from proven source instead, pass `--admit`: before
each run `launch.sh` runs [`admit.py`](admit.py) on the target in place,
admitting `proof fn` bodies + inline `proof { ... }` blocks while
preserving `spec fn` definitions, exec code, and `axiom_*`. `--admit-mode`
is `auto` (default — `lemmas/` & `specs/` → `fn-bodies`, else →
`proof-blocks`), `fn-bodies`, `proof-blocks`, or `both`. It is opt-in and
idempotent, but **resets any proofs already in the file** — point it at a
clean checkout. `admit.py` also runs standalone:
`python admit.py <file.rs> --in-place`.

### Creating a clean admitted worktree

A run wants the target in its **admitted starting state** — `proof fn`
bodies replaced by `admit()`, with `spec fn` defs, exec code, and
`axiom_*` left intact — inside an isolated checkout so the run never
dirties your main tree. That admitted state is what inference-dalek's
**`construct_admitted_state()`** (`inference_dalek/eval/starting_state.py`)
builds and commits to the **`eval/admitted-start`** ref; its sibling
`StartingStateManager.checkout()` is the `git worktree add` half. This
repo's [`admit.py`](admit.py) is the in-repo, single-file counterpart:
`create_admit_worktree()` / `admit.py --worktree` does the checkout, and
its body pass (also `launch.sh --admit`) mirrors the admission.

The dalek-lite project is a Cargo **workspace**: the worktree is added at
the git **repo root** (`.../dalek-lite`, which holds the workspace
`Cargo.toml`); the `curve25519-dalek/` **member** subdir is what you pass
as `--project`. Two ways to get the worktree, both verified end-to-end:

```bash
REPO=/path/to/dalek-lite                 # project git repo root (the Cargo workspace)

# A — check out the pre-built admitted ref (skeleton already committed):
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref eval/admitted-start
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek           # edwards.rs already has 92 admits

# B — build the skeleton from clean proven source. --detach is implicit, so this
#     works even though the primary checkout already holds `main`; --admit-target
#     admits that file in place after checkout (0 → 92 admits for edwards.rs):
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref main \
    --admit-target curve25519-dalek/src/edwards.rs
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek

# tear the worktree down when done
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --remove
```

`create_admit_worktree` is just `git worktree add --detach <dest> <ref>`
plus the optional in-place body pass, so the by-hand equivalent is
`git -C "$REPO" worktree add --detach /tmp/dalek-wt <ref>` then
`launch.sh --admit` / `python admit.py <file> --in-place`. The body pass
**resets any proofs already present**, so it must run on a clean checkout
— a fresh worktree is exactly that. A reuses the committed
`construct_admitted_state` output; B reconstructs the body pass locally.

On a **brand-new worktree**, warm vstd once before the first run —
`cargo verus verify -p curve25519-dalek` from the member dir (~40s) —
otherwise the first module-scoped `verus_check` spuriously fails
(`--verify-module` leaks into an uncompiled vstd → "could not find
module").

**Running several at once.** Each worktree is a fully isolated checkout,
so you *can* fan out parallel runs — give each its own worktree **and**
its own `--results` dir, then one `launch.sh` per worktree. The worktree
clears cargo's `.cargo-lock` (builds serialize on one project root); the
separate `--results` clears the `failure_memory.json` /
`proven_registry.json` / `catalog_cache.json` read-modify-write race
(those are keyed off the results root, not the project). Sharing either
reintroduces a race:

```bash
git -C "$REPO" worktree add --detach /tmp/dalek-wt-1 eval/admitted-start
./launch.sh --detach --run-id par_edw  --project /tmp/dalek-wt-1/curve25519-dalek \
    --vstd-root "$VSTD" --results results-edw  src/edwards.rs
git -C "$REPO" worktree add --detach /tmp/dalek-wt-2 eval/admitted-start
./launch.sh --detach --run-id par_rist --project /tmp/dalek-wt-2/curve25519-dalek \
    --vstd-root "$VSTD" --results results-rist src/ristretto.rs
# watch:  tail -f launcher_par_*.log | grep --line-buffered '^MARKER'
```

## Output

```
results/<run_id>/<target_id>/
├── result.json              # success, end_reason, rounds_used, duration
├── round_1.json             # per-round: verus_okay, errors, spec_drift, usage
├── round_2.json
├── prompt_rendered.md       # exact prompt Claude received (for reproducibility)
├── spec_snapshot.json       # signature baseline (spec_check reference)
└── claude_raw/
    ├── round_1.jsonl        # raw Claude stream-json
    └── round_2.jsonl
```

Aggregate state across runs:

```
results/
├── failure_memory.json      # per-(module,function) prior failures
└── proven_registry.json     # cumulative list of proven targets
```

## Observability

Follow the live CLI-skill log for a running task:

```bash
tail -f results/<run_id>/<target_id>/cli.log
```

Inspect prior failures for a target (useful when iterating on the same
function):

```bash
python lib/failure_memory.py ./results --function field_add_spec
```

Render prior failures as the markdown block the prompt will receive:

```bash
python lib/failure_memory.py ./results --function field_add_spec --as-prompt
```

## Extending

**Add a new search skill** — drop a new file under `skills/` matching
the pattern of `search_semantic.py`: CLI, JSON output, log to
`$CLI_LOG_PATH`. Mention it in `prompt.md` and `skills/SKILL.md`.
That's the entire extension protocol.

**Add a new result field** — add a field to `RoundResult` or `TaskResult`
in `lib/results.py`. Dataclasses; fields persist automatically via the
`asdict()` path.

**Change the prompt** — edit `prompt.md`. `run.py` reads it fresh each
round.

See `docs/extension_spec.md` for the five larger features deliberately
deferred. Each has a trigger ("add this when you observe X") — don't
add them on speculation.

## Debugging

- **"claude not found"**: make sure `claude` CLI is on PATH. `which claude`.
- **"Cargo.toml not found"**: pass `--project` explicitly, or run inside
  the project.
- **"verus: command not found"**: Verus isn't installed or `cargo verus`
  isn't a cargo subcommand in your environment.
- **Agent keeps hallucinating lemma names**: nudge it to use
  `search_semantic.py`, `search_module.py`, and `search_macro.py`, then inspect
  the returned signatures before editing. Check `cli.log` for which search
  skills it actually used.
- **Spec drift fires every round**: the prompt isn't strict enough about
  "don't touch specs" — emphasize rule #1 more; or this is the trigger
  to add the E5 full tracker with auto-restore.

For deeper triage (fake-green runs, rlimit / verus-timeout failures,
axiom-by-convention LIMITs, premature COMPLETE, etc.), see
[docs/diagnostics.md](docs/diagnostics.md) — pattern catalog with
detection commands and fixes.

## Philosophy

Every feature in the MVP addresses a documented HAB pain point from
inference-dalek's `EXPERIMENTS.md`. Nothing is speculative. Every
feature in `docs/extension_spec.md` is documented with its own trigger
— the symptom that would justify building it.

If something breaks, the fix should be local: one skill file, one
section of `run.py`, or one part of the prompt. If a fix requires
changes in more than two files, that's a sign of drift; pause and
reconsider the design.
