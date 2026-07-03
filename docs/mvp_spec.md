# Dalek-Lite-MVP — Spec

## Context

A slim Verus proof-synthesis agent for the dalek-lite codebase. The core design
is still one driver loop, inspectable JSON state, and standalone CLI skills; the
current code has grown enough experiment-mode machinery that "read one loop"
now means following `run.py`'s single round pipeline rather than a 250-line
prototype.

Informed by inference-dalek's 20+ experiments and Numina-Lean-Agent's architecture,
but aggressively narrowed: one persistent session, one coordinator, one target
run at a time. The harness does not orchestrate subagents; the model may use its
own tools, but `run.py` only records and checks each round.

## Scope — what's in the MVP

| Area | Shape |
|---|---|
| **Loop** | `claude -p` session, max N rounds. After each round: run integrity gates, run Verus, detect `END_REASON`, continue or stop. |
| **Search** (HAB pain-point #1: LLM didn't have the theorems) | 4 search skills sharing one canonical catalog — semantic, module, macro, proven |
| **Verification** | `verus_check` — module-scoped `cargo verus --verify-module` for fast local checks, or `--whole-crate` for experiment modes whose pins live outside the target. JSON output includes grouped `summary` and full `messages[]`; whole-crate checks use a 900s default. |
| **Integrity gate** | `spec_check` — before/after signature diff; fails the round if any `fn` signature / `requires` / `ensures` / `decreases` drifted. Whole-crate experiment modes also use tooling, axiom, and frozen-file/change-scope gates. |
| **Failure memory** | Per-function persistent JSON (`results/failure_memory.json`). Injected into the prompt on retry. |
| **Result layout** | Numina-style: `results/<run_id>/<target_id>/{result.json, round_N.json, claude_raw/round_N.jsonl, cli.log}` |
| **Prompt** | One file (`prompt.md`) rendered with scope placeholders. Ordinary runs are target/sibling scoped; whole-crate modes treat the target as a harness anchor and the editable list as the assignment. Lists forbidden constructs (`external_body`, `assume`, spec weakening, new `admit()`). |

## Scope — what's deferred

See `extension_spec.md`. Five features, each documented with the pain it addresses, the trigger that would justify building it, a design sketch, and integration points:

1. Multi-level cascade (L1 widen + L3 human admit)
2. Subagents / autoproof coordinator mode
3. CHECKLIST.md multi-module campaign state
4. Informal-prover skill (Gemini-backed NL proof refinement)
5. Full spec integrity tracker (auto-restore on drift)

## The loop

```
run.py target.rs [--rounds 5] [--run-id foo]
  ├─ snapshot signatures (spec_check --snapshot)
  ├─ round 1:
  │    ├─ claude -p --verbose --output-format stream-json prompt.md
  │    │    ├─ Claude edits target.rs
  │    │    └─ Claude calls skills/{verus_check, search_*} as needed
  │    ├─ save claude_raw/round_1.jsonl
  │    ├─ spec_check --verify  → gate: did agent weaken specs?
  │    ├─ tooling/axiom/frozen-file gates as configured
  │    ├─ verus_check target.rs, or verus_check --whole-crate for whole-crate modes
  │    └─ record round_1.json
  ├─ round 2: claude -c "continue" (reusing session state)
  │    ...
  └─ on terminal END_REASON or N rounds: write result.json, update failure_memory.json
```

`COMPLETE` is recorded only when the final-state gate proves Verus is okay for
the configured gate scope, no integrity gate drifted, and zero hard `admit()`
remain in scope. That gate can promote an over-cautious `LIMIT` / empty claim to
`COMPLETE` when the worktree is truly green; infrastructure failures such as
HTTP 429, retry exhaustion with no Claude result event, and transport exits stay
explicit non-promotable outcomes (`RATE_LIMITED`, `RETRY_EXHAUSTED`,
`TRANSPORT_ERROR`) rather than proof LIMITs.

## File inventory

```
dalek-lite-mvp/
├── run.py                    # the single driver / round loop
├── prompt.md                 # the task prompt
├── README.md                 # how to use
├── docs/
│   ├── mvp_spec.md           # this file
│   └── extension_spec.md     # the 5 deferred features
├── lib/
│   ├── catalog.py            # ~200 LOC — canonical catalog builder
│   ├── failure_memory.py     # ~60 LOC — per-function persistent record
│   └── results.py            # ~80 LOC — result-dir helpers
└── skills/
    ├── verus_check.py        # ~100 LOC — cargo verus wrapper → JSON
    ├── spec_check.py         # ~120 LOC — snapshot / verify signature integrity
    ├── search_semantic.py    # ~60 LOC — keyword/substring over catalog
    ├── search_module.py      # ~60 LOC — pull all sigs from one module
    ├── search_macro.py       # ~90 LOC — static expansion of lemma_*! macros
    └── search_proven.py      # ~40 LOC — ProvenRegistry reader
```

The implementation is intentionally still "flat": one driver file plus
standalone stdlib-only skills. New behavior should normally land as a small
extension to that loop or as a new CLI skill, not as an orchestration hierarchy.

## Success gate

Running the MVP against a simple dalek-lite module (e.g., one of the Layer 0 modules) should:
1. Drive `verus` to success in 1-3 rounds for easy targets (≤20 LOC proof bodies)
2. Refuse to commit on any spec drift (spec_check exit ≠ 0 → round fails)
3. Leave a readable, round-by-round trace in `results/<run_id>/`
4. Populate `failure_memory.json` with one entry per failed attempt, keyed by `(module, function)`

No claim about end-to-end HAB performance yet — that comes after the MVP is stable enough to run EXP-013 against.

## Non-goals (explicit)

- **No batch runner.** One target per invocation. Script over it with bash if you want a campaign.
- **No autosearch / multi-agent mode.** Single Claude session. Subagent spawning via Task tool is available to the model but not orchestrated by the harness.
- **No cascade levels.** If round N fails, round N+1 gets the error feedback + failure memory and tries again. No widening, no decomposition, no escalation.
- **No web UI / dashboard.** Results are JSON files — `jq` and `less` are the UI.
- **No cross-module campaign state.** Each run is independent; the only persistence is `failure_memory.json`.

## Why this shape

- **One file per skill** → easy to extend. Adding a 6th search skill is a new 60-LOC file, not a schema negotiation.
- **Skills are plain CLIs** → debuggable standalone. You can `python skills/search_semantic.py "lemma_pow2"` at the shell and see what the LLM will see.
- **State on disk, not in Python objects** → inspectable mid-run. `cat results/<run_id>/failure_memory.json` tells you everything the memory subsystem has.
- **`claude -p` over SDK** → no Anthropic/OpenAI dep in `pyproject.toml`. Claude Code handles caching, tool use, session continuation.
- **No custom orchestration classes** → `VerusAgent`, `RepairLoop`, `RecoveryCascade` are all collapsed into one 250-LOC `run.py`. If a round-handling question arises, there's exactly one place to look.
