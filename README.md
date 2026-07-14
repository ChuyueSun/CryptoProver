# CryptoProver

**CryptoProver** is an AI-based system that writes formal specifications and
proofs for cryptographic Rust libraries. A human supplies the high-level API
contracts that say what the library must do and a trusted collection of
low-level arithmetic facts. The agent writes the internal specifications and
proofs between them, and [Verus](https://github.com/verus-lang/verus) checks
the result. The executable Rust code stays unchanged.

This repository accompanies the paper **"AI Approach to Production
Cryptographic Libraries."** The paper asks whether an AI agent can work at
the scale of a production crate, where one proof depends on definitions and
lemmas spread across many files. Most earlier proof-synthesis evaluations give
the model one theorem, function, or module with the relevant specification
already in scope. CryptoProver instead treats the library as the unit of work.

> CryptoProver proves code relative to the supplied contracts and trusted
> facts. It does not decide whether those contracts fully capture the intended
> cryptographic protocol.

## What CryptoProver does

1. **People define the boundary.** Public API contracts describe the conditions
   callers must meet and what each function guarantees. The trusted floor
   provides field and number-theory facts that the experiment does not ask the
   agent to re-prove.
2. **The agent fills in the middle.** Within a fixed vocabulary of logical
   definitions, it writes the intermediate specifications, helper lemmas, and
   proof code needed to connect the API to that floor.
3. **Verus checks the crate.** A run succeeds only when the required scope
   verifies, no unfinished `admit()` remains, and every integrity check passes.

The system does not accept a green verifier result at face value. An agent
could make a proof easier by weakening a specification, adding an axiom,
changing the checker, breaking a neighboring module, or recovering a reference
proof. CryptoProver checks for these failure modes after every round and runs
the agent in an isolated environment without the reference proof.

## Results in brief

We evaluated CryptoProver on two production cryptographic libraries without
changing their executable code:

- **dalek-lite.** CryptoProver reconstructed the internal specifications and
  proofs of an independent, human-led verification. That public effort spanned
  eight months and five main contributors, including specification and
  infrastructure work. Given the API contracts and trusted floor,
  CryptoProver completed the reconstruction in 11.4 agent-hours and $466.99
  of recorded API cost. The final crate verified on two machines with no
  integrity violations.
- **RustCrypto ChaCha20.** CryptoProver verified a previously unverified
  RFC 8439 soft-core fork while leaving the executable implementation
  unchanged.

The detailed dalek-lite evidence is available in the checked-in run records:

- [Field-floor certificate](docs/run_stats/stage3_certificate_record.md): the
  primary `claude-fable-5` run reached whole-crate zero errors in two attempts
  and 11.4 agent-hours.
- [Cross-model replication](docs/run_stats/stage3_opus48_arm_record.md): a
  `claude-opus-4-8` run reached the same whole-crate result in nine attempts and
  62.3 agent-hours.
- [Earlier non-convergent campaign](docs/run_stats/final_convergence_stats.md):
  the failure baseline that motivated the later staged method.
- [CryptoProver-core](https://github.com/ChuyueSun/CryptoProver-core): the
  companion proof-only experiment, which kept all specifications fixed and
  removed proof bodies across the crate.

This repository contains the full harness used for internal-specification and
proof synthesis. `CryptoProver-core` contains the smaller proof-only harness.
This checkout is a cleaned public snapshot: campaign statistics are included
under [`docs/run_stats/`](docs/run_stats/), but raw run artifacts and internal
working notes are not.

## How a run works

```
fixed code + API contracts + specification vocabulary + trusted floor
                              │
                              ▼
                    agent writes specs and proofs
                              │
                              ▼
                    Verus checks the requested scope
                              │
                              ▼
                  integrity gates inspect the changes
                              │
                              ▼
                 COMPLETE only if every check passes
```

`run.py` is the driver. It renders `prompt.md`, starts a headless Claude Code
session, and checks the resulting tree after every round. The checks cover:

- weakened contracts or changed specification definitions;
- new axioms, `assume(...)`, or `#[verifier::external_body]` escapes;
- unfinished `admit()` calls;
- failures in touched files, sibling files, or the whole crate;
- edits to frozen source files or to the checking tools; and
- attempts to recover a reference proof from git history.

For controlled evaluations, `peel.py` creates a starting state with selected
internal specifications or proofs removed. A JSON manifest records exactly
what the agent receives, what it may edit, and what stays frozen. The
field-floor manifest defines the reconstruction used for the paper's primary
dalek-lite result. See [`peel_manifests/README.md`](peel_manifests/README.md)
for the exact boundaries.

## Reproducing the paper results

Inspect first — the shipping records are the claim:

- [docs/run_stats/stage3_certificate_record.md](docs/run_stats/stage3_certificate_record.md)
  — the certificate: cut, measured pins, sterility evidence, both attempts,
  the verification battery, and the result statement quoted above.
- [docs/run_stats/stage3_opus48_arm_record.md](docs/run_stats/stage3_opus48_arm_record.md)
  — the opus-4.8 replication arm: nine-attempt table, endgame trace,
  certificate battery, disclosures, and the fable-5 comparison.
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
