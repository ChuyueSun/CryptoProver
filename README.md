# CryptoProver

**CryptoProver** is a *gated* LLM proof-synthesis agent for
[Verus](https://github.com/verus-lang/verus)-annotated Rust: it writes the
machine-checked specifications, lemmas, and proofs that connect a
cryptographic crate's public API to the arithmetic beneath it. Because a
green verifier run is cheap to fake, the agent's one small driver loop is
wrapped in integrity gates that make every success claim mechanically
checkable. This is the artifact accompanying the paper **"Building a
Verified Cryptographic Crate with a Gated LLM Agent"**, evaluated on
dalek-lite (a Verus port of curve25519-dalek).

Two harness versions exist:

- **This repository** — the full harness: spec-synthesis experiment modes,
  the **peel** init-state builder (graded cuts along a depth × span design),
  and whole-crate gating. This version ran the paper's deepest cut — the
  **field-floor certificate** below.
- **[`CryptoProver-core`](https://github.com/ChuyueSun/CryptoProver-core)** —
  the slim companion harness (proof bodies only, under fixed specifications),
  which ran the paper's **coverage cut**.

> This repository is a cleaned public snapshot of the research tree. Run
> artifacts and internal working notes are not included; the campaign statistics
> backing the paper live in `docs/run_stats/`.

## Headline results

**1. The field-floor certificate (this harness).** From the canonical run
record ([docs/run_stats/stage3_certificate_record.md](docs/run_stats/stage3_certificate_record.md)):

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

**Cross-model replication.** A second, independent arm on the identical cut
— `claude-opus-4-8` under the same harness and gates, H0/unassisted —
also converged to **whole-crate 0 errors** (2,114 declarations verified;
harness-sealed COMPLETE plus a fresh-container re-verify), taking 9
seed-chained attempts / 62.3 agent-hours vs fable-5's 2 attempts / 11.4 h.
Both arms hit the same hardest obligation (the ristretto batch-compress
loop) and independently rediscovered the ground truth's resolution —
fine-grained lemma decomposition over SMT-budget raises. Full record with
per-attempt table, integrity batteries, and disclosures:
([docs/run_stats/stage3_opus48_arm_record.md](docs/run_stats/stage3_opus48_arm_record.md)).

**2. The coverage cut (companion harness).** With every specification kept
and every proof body stripped crate-wide, the accompanying paper reports the
agent closed 1,430 of 1,433 non-axiom proof obligations (3 gaps left, all
shared with the human reference) and re-verified the whole crate, with zero
fabricated axioms and no active-gate firings. Those numbers are
paper-sourced; that run's harness and records live in the companion
[`CryptoProver-core`](https://github.com/ChuyueSun/CryptoProver-core) repository.

## How it works

```
prompt.md ──rendered──▶ claude -p   (agent edits proofs, runs skills/*.py via Bash)
                            │
        each round: spec_check verify ▸ cargo verus ▸ integrity gates
                            │
        COMPLETE only if: verifier green in scope
                          AND zero hard admit()s remain
                          AND no integrity gate drifted
```

One driver loop (`run.py` — no orchestration class hierarchy) renders
`prompt.md`, invokes the Claude Code CLI headless, and after every round
re-verifies the tree and re-checks a bank of integrity gates. The four core
gates — each traced to a false-success mode observed in a real campaign:

- **Spec integrity** (`SPEC_DRIFT`) — every `fn` header, `requires`/`ensures`,
  and (when enabled) every `spec fn` *body* is snapshot-pinned before the run;
  any drift fails the round. Weakening the spec is the cheapest fake green.
- **Axiom integrity** (`AXIOM_DRIFT`) — a new `axiom_*` name vs the baseline
  fails the round (axiom admits are excluded from the done-counter, so a new
  axiom would otherwise be a free postcondition).
- **Tooling integrity** (`TOOLING_DRIFT`) — every harness/skill file is
  SHA-pinned; a doctored checker fails the round.
- **Forbidden constructs** (`FORBIDDEN_CONSTRUCT`) — any new `assume(...)` or
  `#[verifier::external_body]` fails the round.

Further checks back these up — final-state admit counting, the frozen-file
guard for whole-crate modes, sibling-file scans, spec-definition freezing —
see `CLAUDE.md` for the full bank.

**Peel** (`peel.py` + `peel_manifests/`) builds the graded starting states: a
JSON manifest names, per file, what to strip along a cumulative depth axis —
P1 proof bodies → P2 helper lemmas → P3 spec definitions → P4 contracts —
and every file not listed stays frozen. A pin rule refuses unsound cuts (a
P3/P4 strip must declare what still constrains the reconstruction). The
**field-floor** manifest is the deepest evaluated cut.

## Reproducing the paper results

Inspect first — the shipping records are the claim:

- [docs/run_stats/stage3_certificate_record.md](docs/run_stats/stage3_certificate_record.md)
  — the certificate: cut, measured pins, sterility evidence, both attempts,
  the verification battery, and the result statement quoted above.
- [docs/run_stats/](docs/run_stats/) — the surrounding campaign records,
  including the June non-convergence baseline that motivated the staged
  methodology.

To rebuild the cuts yourself (requires the dalek-lite benchmark repo — see
Prerequisites — and `docs/spec_gen_runbook.md` for full setup):

```bash
# preview the field-floor cut: what gets deleted/stripped, and the pin
./peel_run.sh --manifest peel_manifests/field_floor.json --surface

# build the peeled worktree and launch the proof run in one command
./peel_run.sh --manifest peel_manifests/field_floor.json --run-id ff_001 --detach
```

Cheaper entry points: the decompress rungs cut a single API path instead of
the whole cone — `peel_manifests/decompress_proof_only.json` (P1),
`decompress_contract_only.json`, `decompress_bridge_specs.json` (P3), and
`decompress_bridge_full.json` are ready-made; `peel_manifests/README.md` maps
each to its experiment mode and soundness pin.

**Containerized runs.** [docker/](docker/README.md) ships an immutable image
(pinned Rust + Verus + Z3 + the harness, baked-warm cargo caches) and a
launcher (`docker/run_agents.sh`) for isolated one-container-per-target
sweeps — sealed worktree per agent, shared CPU pool, `--tap` tracing and
`--seed-wip` resume options. Image build, sealing, and a full single-container
proof round are smoke-tested; a full parallel multi-container sweep has not
been run yet. See [docker/README.md](docker/README.md).

Documentation map:
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
├── peel.py             # init-state builder: manifest → graded peeled cut
├── peel_run.sh         # one command: manifest → peeled worktree → run.py
├── peel_manifests/     # the cuts (field_floor.json, decompress_*.json, …)
├── admit.py            # admit() skeleton builder + isolated worktrees
├── docker/             # containerized parallel sweeps (see docker/README.md)
├── strip_specs.py      # spec/lemma strip verbs used by peel
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

### Layer Set A (the field-layer benchmark)

Layer Set A = **L0 + L1 = 9 modules** (4 field-repr modules + 5 field-reduce
modules). These field-representation and -reduction modules are small and
self-contained — a good first benchmark for the core agent.

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

Layer sets A/B/C/D are all wired in — A: field repr + reduce
(9 modules), B: serialize (6), C: edwards base + ops (15), D: ristretto (5). Pass the letter as the
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

A run wants its target in the **admitted starting state** (`proof fn` bodies
replaced by `admit()`; `spec fn` defs, exec code, and `axiom_*` intact) inside
an isolated checkout, so the run never dirties your main tree:

```bash
REPO=/path/to/dalek-lite
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref eval/admitted-start
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --remove   # teardown
```

Full detail — building the skeleton from proven source instead, the Cargo
workspace layout, vstd warm-up on fresh worktrees, and safe parallel fan-out
(one worktree + one results dir per run) — lives in
[docs/admitted_worktrees.md](docs/admitted_worktrees.md).

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

Every feature in the core agent answers a pain point documented in the
predecessor system's experiment log. Nothing is speculative. Every feature
in `docs/extension_spec.md` is documented with its own trigger — the
symptom that would justify building it.

If something breaks, the fix should be local: one skill file, one
section of `run.py`, or one part of the prompt. If a fix requires
changes in more than two files, that's a sign of drift; pause and
reconsider the design.
