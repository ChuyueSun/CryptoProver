# Domain Spec: dalek-lite-mvp (Verus Proof Synthesis)

## Domain Summary

**Task.** Replace `admit()` calls in a Verus-annotated Rust module with real proofs that pass `cargo verus`. One evaluation unit is one `.rs` target file: the agent gets N rounds of `claude -p` to produce a proof, with a spec-drift gate (specs must not be modified) and an admit-count gate (zero non-axiom admits remaining). Outcome is binary `TaskResult.success`.

**Fixed components.**

- The runner [run.py](run.py) — round loop, signal handling, spec-drift gate, verus check, admit-count gate.
- The base model — defaulting to **Claude Sonnet** (configurable via `--model`). Held fixed across a search run to attribute gains to the harness.
- The benchmark task set — 35 Verus modules across Layers A/B/C/D, mirrored from `inference-dalek/eval/domain_layers.py`.
- The eval boundary — `verified / total` reported in `layer_summary.json`.

**Allowed to change (the harness).**

- [prompt.md](prompt.md) — primary lever.
- `skills/*.py` — agent-facing CLIs (`verus_check`, `spec_check`, `search_semantic`, `search_module`, `search_macro`, `search_proven`, `diff_view`, …). New skills may be added.

**Out of scope for the first search loop.**

- Round-loop policy in `run.py` (round count, gate logic, retry strategy).
- The base model (no model search; would confound harness gains).
- Tooling outside `dalek-lite-mvp/` (verus version, dalek-lite source).

**Budget.** Tight: under **$50 and under 1 hour per iteration**. Concretely: Layer A only (9 tasks), 1 trial per task, Sonnet, default round cap. This caps proposer + benchmark per iteration.

## Harness and Search Plan

**Candidate harness shape.** A candidate is a **directory** containing the two swappable artifacts:

```
candidates/<name>/
  prompt.md
  skills/             # full skills dir, including any new/modified scripts
  hypothesis.md       # optional: one-paragraph rationale for the diff vs. baseline
```

No Python class boundary needed — `run.py` already takes prompt+skills implicitly from the working directory. Interface compliance is checked by:

1. **Smoke check**: run the harness on one cheap task (e.g. `field_specs`) and confirm the round loop completes without crashing.
2. **Spec-drift check**: the existing gate in `run.py` already rejects any candidate whose prompts or skills cause spec modification.

**Baselines.**

- `baseline`: copy of the current `prompt.md` + `skills/` as of this spec date (2026-05-24). Re-baseline if the upstream MVP changes.
- Possibly `baseline_no_search_proven`: ablation that disables the cross-run proven-lemma registry — useful to surface candidates that overfit to memorized lemmas.

**Reusable helpers to factor out from day one.**

- `swap_candidate(name)` and `restore_baseline()` — rsync candidate files into the dalek-lite-mvp working tree and back.
- `run_layer_a(candidate_name) -> dict` — wraps `run_layer.py`, parses `layer_summary.json`, returns `{verified, total, per_task: {...}}`.
- `parse_result_json(path) -> RoundResult-summary` — for the proposer's prior context.

**First search loop (per iteration).**

1. Proposer reads `evolution_summary.jsonl` + `frontier.json` + recent failures from `results/` and prior candidates.
2. Proposer writes one or more new `candidates/<name>/` directories.
3. Smoke check each candidate on `field_specs`.
4. Full Layer A eval for each survivor. Record `verified/9` and per-task pass/fail.
5. Update frontier (highest `verified` on Layer A; ties broken by lower mean `rounds_used`).

**Final held-out pass.** After the search budget is spent, run the frontier candidates on Layers B/C/D (26 tasks). Report `verified/26` per held-out layer. **Do not feed held-out outcomes back into the proposer.**

## Evaluation Plan

**Search set.** Layer A — 9 modules: `field_specs`, `field_specs_u64`, `reduce_lemmas`, `add_lemmas`, `negate_lemmas`, `mul_lemmas`, `pow2_51_lemmas`, `compute_q_lemmas`, `u64_5_as_nat_lemmas`. Primary signal: `verified / 9`.

**Held-out test set.** Layers B (6 modules), C (15 modules), D (5 modules). User confirmed no shared structural dependencies between A and B/C/D, so the split is clean at the module level. **One residual leakage path to gate (see below).**

**Primary metric.** `verified / total` — fraction of modules where `TaskResult.success == True` (verus passes AND zero non-axiom `admit()` AND no spec drift).

**Secondary metrics (reported, not optimized).**

- Mean `rounds_used` per success — efficiency proxy.
- Mean `duration_seconds` per task — wall-clock cost.
- API spend per Layer A run — dollar cost proxy.
- End-reason distribution: `COMPLETE` / `LIMIT` / `SPEC_DRIFT` / `ERROR`. `SPEC_DRIFT` candidates should be rejected outright, not just ranked low.

**Noise.** `unknown`. Claude is sampled with temperature, and the round count is policy-dependent, so two runs of the same candidate can disagree on individual modules. **Proposed default: run baseline 3 times on Layer A before the first iteration to establish a per-module flip rate; use the mean and SD as the noise floor.** Re-measure if the floor changes search decisions.

**Per-candidate runtime.** Target ≤1h on Layer A; abort a candidate run if it exceeds 90 minutes.

**Contamination / leakage risks.**

1. **`proven_registry.json` cross-run leakage.** A success on a Layer-A module can deposit a proven lemma into `results/proven_registry.json`, which `search_proven.py` then surfaces to *any* future run, including held-out. **Mitigation:** wipe (or freeze) `proven_registry.json` and `failure_memory.json` before the held-out pass, so held-out evaluates the candidate in isolation. Also: snapshot these files at iteration start to make held-out reproducible.
2. **Memorization in proposer context.** Past `result.json` files in `results/` may contain solved proofs from B/C/D. **Mitigation:** when assembling the proposer context, only include `result.json` files from Layer A runs; explicitly exclude `results/layer{B,C,D}*` paths.
3. **Re-running the same harness on the same task** within a search loop will pick up its own prior `proven_registry` deposits and look better than a cold run. **Mitigation:** start each candidate's Layer A evaluation from a snapshotted registry, not the running one.

**Cheap validation checks.** Before counting a candidate as successful, re-run `cargo verus` on the produced artifact directly (don't trust the agent's claim).

## Experience and Logging

**Per-candidate raw traces to store** under `meta_harness/logs/<run_name>/candidates/<candidate_name>/`:

- `prompt.md` and `skills/` (snapshot of the candidate itself).
- `hypothesis.md` (proposer's one-paragraph rationale).
- `layer_summary.json` — top-line `verified/9` + per-task summary.
- Per-task `result.json` (already produced by `run.py`).
- Per-task `claude` session transcripts — highest-signal artifact for debugging *why* a candidate fails.
- Per-task `verus_check.py` outputs.
- `metadata.json`: candidate name, proposer iteration, propose time, bench time, dollar cost, base-model id, snapshotted `proven_registry.json` hash.

**Highest-signal debugging artifacts** (in priority order):

1. Claude session transcript for failed tasks — reveals whether the model is confused about the prompt or about Verus.
2. `result.json` `end_reason` field — distinguishes timeout vs. spec-drift vs. error.
3. Per-round `RoundResult.verus_okay` trajectory — reveals whether progress was monotonic.

**Directory layout.**

```
~/dalek-lite-mvp/meta_harness/
  benchmark.py
  meta_harness.py
  .claude/skills/meta-harness-dalek/SKILL.md
  candidates/
    baseline/
    <generated>/
  logs/
    <run_name>/
      evolution_summary.jsonl      # one row per candidate evaluation
      frontier.json                # current Pareto / best-so-far
      candidates/<name>/...        # per-candidate artifacts as above
      proven_registry.snapshot.json
```

**CLI for querying run history.** Worth building once there are >5 candidates. Minimum commands:

- `benchmark.py --results` — print frontier table (candidate, layer-A score, delta vs. baseline, rounds_used).
- `benchmark.py --diff <name>` — diff a candidate's `prompt.md`/`skills/` against `baseline`.
- `benchmark.py --task <module>` — show per-candidate outcomes on one Layer A module (cheap signal for "which candidates moved this needle").

**Offline experience to seed the proposer skill.**

- Existing `results/layer{A,B,C,D}_*` directories — but **filter to Layer A only** for the proposer context (see leakage mitigation #2).
- `failure_memory.json` — patterns of past round-level failures.
- The Meta-Harness paper at https://arxiv.org/abs/2603.28052 for the framework prior.
- The dalek-lite-mvp [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md) / [README.md](README.md) for architecture.

## Open Questions and Unknowns

- **Exact base model.** `unknown`. Default proposed: Claude Sonnet (matches tight-budget constraint). Decide before the first iteration; pin in `meta_harness.py`.
- **Evaluation noise floor.** `unknown` until baseline is run 3x on Layer A.
- **Round cap per task.** Currently `--rounds 5` in `run.py`. Held fixed for the search; revisit if many candidates hit `LIMIT` rather than `COMPLETE`.
- **Concurrency.** Layer A has 9 tasks; reasonable to run them concurrently up to API tier limits. Default 3 — adjust based on observed timeouts.
- **Whether to allow new skill files vs. only edits.** Letting the proposer add new `skills/*.py` widens the search space but increases the chance of broken candidates. Start by *allowing* it; gate via smoke check.
- **Re-baselining cadence.** The "baseline" candidate is a snapshot. If the upstream `dalek-lite-mvp/prompt.md` or `skills/` changes during a search run, the delta numbers become incoherent. Freeze the working tree (or take a git tag) at run start.
- **Whether `proven_registry.json` should be *off* during search.** If it's on, candidates that get lucky early benefit from a richer registry on later tasks within the same iteration. Cleaner: snapshot at iteration start, restore between candidates so each candidate sees the same registry state.
