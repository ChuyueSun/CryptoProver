# Dalek-Lite — Extension Spec

Five features deliberately left out of the MVP (**E1**–**E5**), plus three later
patterns that were *not* left out but earned a documented home on the `spec_gen`
branch: **E6** (spec-inference experiment mode, built), **E7** (false-contract
classification, trigger fired), and **E8** (scope decomposition, trigger fired).
For each: **pain it addresses**, **trigger that would justify adding it**, **design
sketch**, **integration points**, **rough LOC cost**. None of these should be added
speculatively — each one adds surface area, and inference-dalek's history
(FEATURES_ANALYSIS.md: 15/19 features dead-code / buggy / net-negative) shows what
happens when features are built before they earn their place.

---

## E1. Multi-level cascade

### Pain it addresses

MVP has one level: edit-verify-retry with error feedback. When the LLM can't solve a function that way, the only options are "try again" (pointless if context didn't change) or "give up."

Inference-dalek's history shows two specific failure modes this doesn't cover:

- **Sibling errors** — fixing function `foo` introduces a Verus error in sibling `bar` in the same module. The LLM needs to see `bar` to understand; but if it edits `bar`, committed proofs get clobbered (fixed in commit `147a106`). MVP avoids by forbidding off-target edits in the prompt, but then some proofs are genuinely unfinishable at level 0.
- **Large bodies (>50 LOC)** — direct generation drops to ~50% success vs ~95% for ≤50 LOC (per pipeline_complete.md). Without decomposition, large modules are de-facto blocked.

### Trigger

Add when the MVP fails to make progress on ≥2 modules where post-mortem shows "the LLM couldn't see sibling context" or "the proof body exceeded ~80 LOC." Track via `results/<run_id>/<target>/result.json` `end_reason` and a tag `failure_mode`.

### Design sketch

Three levels, level stored in `round_N.json.level`. LLM sees current level in prompt.

```
L0 (MVP baseline):
  scope read = target fn only
  scope write = target fn only
  attempts = 3
  exit up on: verus ok
  exit across to L1 on: 3 attempts exhausted OR error line outside target fn

L1 (widen + surgical merge):
  scope read = full module
  scope write = target fn only (enforced by a new primitive: merge_target_only())
  attempts = 3, each using a different context ordering (sibling-first / obligation-first / strategy-first — from Exp 18a)
  exit up on: verus ok
  exit across to L2 on: 3 attempts, OR LLM reports "I need a sibling edit"

L2 (decompose OR informal — see E4):
  subagent-spawned (see E2)
  output = one or more sub-lemmas, each re-entering L0

L3 (human admit):
  only place admits are written
  writes <module>/DEFERRED.md with structured record
```

### Integration points

- New `scripts/merge_target_only.py` — takes LLM's full-module response and the original module, extracts only the target fn, merges. Uses `syn` or a simple brace-matcher.
- `run.py` gains a `--max-level` flag (default `0` = MVP). Each level is a separate prompt file (`prompt_l0.md` → `prompt_l1.md` → `prompt_l2.md`).
- `failure_memory.json` gains a `last_level_reached` per function — drives "don't re-attempt at L0 if L1 was needed last time."

### Cost

~400 LOC across merge primitive + 3 prompt files + level-transition logic in `run.py`. Roughly doubles the MVP size.

### Status note (`spec_gen`)

A *much lighter* response to this same "try again is pointless / give up is the only other option" pain is already built on the `spec_gen` branch: the **NEEDS_DECOMP escalation** (`END_REASON:NEEDS_DECOMP`). Instead of E1's L0→L3 level machine with a merge primitive and per-level prompt files, the agent emits a single escalation label when a proof is genuinely blocked on **missing infrastructure** (a helper lemma/chain that does not exist, or a sub-lemma split) and names what's missing. The loop breaks on it and records the label; a fresh `run_task` on the same target (e.g. a `run_layer` re-run) detects the prior NEEDS_DECOMP record and retries with +2 rounds, 1.5× wall-clock, and a "build the named infrastructure first" directive prepended to the failure-memory block. It does **not** widen edit scope, merge full-module responses, or spawn subagents — it just front-loads budget on the retry and tells the agent what to build. The final-state decision is the pure `run._final_end_reason` and the parse is `run.END_REASON_RE`, both pinned in `tests/test_admits.py`.

This is a partial answer to E1's `L2 (decompose)` transition only: it surfaces *that* decomposition is needed and names the gap, but the agent still does the decomposition itself in the retry session rather than via a coordinator. If NEEDS_DECOMP fires often *and* the budget-bumped retries still fail to build the named infrastructure unaided, that's the signal to build the real E1 cascade (level state, `merge_target_only`, per-level prompts).

---

## E2. Subagents / autoproof coordinator mode

### Pain it addresses

**Context explosion on long-horizon proofs.** A single Claude session accumulates context over many rounds; by round 10 on a complex module, the context is 100k+ tokens of accumulated trial-and-error. The model's instruction-following degrades (documented in the paper for Putnam A5 — "when the context becomes too long, the model's ability to follow instructions degrades significantly").

Numina's autosearch solves this by spawning subagents via Claude Code's Task tool: the coordinator has a short context (just CHECKLIST.md + subagent return messages); the proof work happens in throwaway subagent contexts that get discarded after they finish.

### Trigger

Add when (a) a target module's session grows past ~50k tokens before finishing, AND (b) post-mortem shows the model forgetting earlier instructions or repeating earlier mistakes. Track via `round_N.json.usage.input_tokens` — if it grows monotonically past 50k, this is the symptom.

### Design sketch

A new `prompts/autoproof/` directory modeled directly on Numina's `prompts/autosearch/`:

```
prompts/autoproof/
├── main_entry.md            # coordinator prompt — reads CHECKLIST, picks target, spawns subagent
└── subagent_prompts/
    ├── common.md            # forbidden: external_body, spec weakening, bare admit()
    ├── coordinator.md       # role spec for coordinator
    ├── proof_agent.md       # prove one function; forbid Task tool from re-spawning
    └── repair_agent.md      # interpret Verus error, propose fix
```

Coordinator runs as the outer Claude session (same `run.py`, different prompt). Each subagent is spawned via Claude Code's Task tool. Coordinator is **forbidden** from reading `.rs` files or calling Verus directly — all proof work delegated.

### Integration points

- `run.py --mode autoproof` switches prompt file from `prompt.md` to `prompts/autoproof/main_entry.md`.
- CHECKLIST.md (see E3) becomes the coordinator's working memory.
- Bounded parallelism: coordinator may spawn ≤2 subagents, must be on different files (prevents edit conflict — same rule as Numina).
- No new skills needed — subagents use the existing skill set.

### Cost

~6 markdown prompt files, ~0 Python. This is "free" once CHECKLIST.md and multi-target runs exist. Numina's autosearch prompts total ~2500 lines of markdown; we can start at ~800 lines because we have fewer roles.

### Critical caveat

From the paper's ablation (Table 2): subagent mode gave the final **12/12 vs 11/12** lift — one extra problem (A5). Most problems don't need it. Don't add it until you see the context-degradation symptom.

### Status note (`spec_gen`)

A *lighter* response to this same pain is already built on the `spec_gen` branch: `run.py --auto-reset` (on by default) starts a fresh `claude` session — dropping accumulated context — when the session's cache-creation tokens cross `--bloat-threshold-tokens`, or when two rounds stall with no admit progress. It does not spawn subagents; it just resets the one session, so it is a cheaper partial answer to E2's context-explosion symptom. If the bloat trigger fires often *and* resets don't recover progress, that's the signal to build real E2 subagents.

**Trigger fired — prompt-level delegation added (`spec_gen_subagent_context` branch).** Context degradation was observed as the dominant failure mode, so a *second* lighter-than-full-E2 lever was added on top of auto-reset, in two parts:

1. **Prompt-level subagent delegation** (`prompt.md`, section "Delegate hard sub-proofs to a subagent"). The single agent is told to stay a thin coordinator and push exploratory churn (multi-file lemma hunts, repeated `verus_check` cycles) into a Task subagent, which burns its *own* context and returns only the final working proof. The harness does **not** orchestrate this — `claude` already runs with `--permission-mode bypassPermissions` and no `allowedTools` restriction, so the Task tool is available; the prompt just encourages its use. This is strictly weaker than the full E2 coordinator design above (no `main_entry.md` / `subagent_prompts/`, no forbidding the coordinator from touching `.rs`, no CHECKLIST working memory) — it is a hint, not an enforced architecture.
2. **Earlier reset backstop.** `--bloat-threshold-tokens` default lowered 300000 → 200000 so the existing auto-reset sheds the session sooner; delegation keeps the parent context lean *between* resets.

This is deliberately the cheapest test of the E2 hypothesis: measure `round_N.json` `cache_creation_input_tokens` growth before/after to see whether delegation curbs the degradation. If it does not — i.e. the model ignores the delegation hint, or context still degrades despite it — *that* is the signal to build the full harness-orchestrated E2 coordinator mode (`--mode coordinator`, `build_coordinator_block`, `subagent_prompts/`) described in the design sketch above.

**Findings (2026-06-01) — corrects two assumptions above.** The prompt-level hint and a full coordinator prototype were tested on the `eval/spec-stripped-vartime` surface. Three results, the third of which reframes the whole feature:

1. **Prompt-only delegation never fired** — across 2 runs / 7 rounds with genuine context pressure, the agent spawned **zero** subagents. Optional encouragement isn't enough; the agent grinds inline.
2. **Tool name:** the subagent tool emits as `Agent` in the headless `claude -p` stream-json (the hint's "Task" wording is tolerated — the model maps it — but `Agent` is the literal name). It is available under `--permission-mode bypassPermissions`.
3. **Subagents do NOT pollute the parent context — so the premise was wrong.** Measured directly: a built-in `Agent` delegation returned **1721 bytes** (one `RESULT:` line) into the parent; the subagent's file reads / Verus output / Z3 dumps stayed in its own context. The bloat seen in a `--mode coordinator` prototype came from the *coordinator reading `.rs` files itself* (14–21 KB `tool_result` blobs), **not** from subagents. So "subagent context isolation" was never the missing piece — the built-in Agent tool already provides it.

Consequently the prototyped `--mode coordinator` and a `prove_obligation` skill (a separate-process prover) were **built then removed**: they duplicated isolation that already exists and did not fix the real problem (the agent doing heavy reads itself, which has no hard enforcement — `--disallowedTools` *propagates to subagents* and is `Bash cat`-bypassable). **Kept:** only the `--bloat-threshold-tokens` 300000→200000 change (validated). **The actual open lever** is behavioral: get the driver to delegate heavy exploration to the (already-isolating) built-in Agent tool instead of grinding inline — prompt-only does not induce this, and there is no harness mechanism to force it short of restructuring proving into separate `run.py`-level processes.

**Compliance rate.** Prompt-only delegation fired **0/7 rounds = 0.0%** (`subagent_check`: 0/2; `subagent_check2`: 0/5 — both standard mode). No published study reports a prompt-only delegation rate, so this is a (small-n) data point for that gap. For contrast, the now-reverted coordinator-mode prototype (`coord_check`) was the *only* configuration that delegated at all — **2 `Agent` spawns over 2 rounds** — confirming delegation needs explicit scaffolding, not a prompt hint.

**Standing plan (read-only offload + compaction):**
1. **Default single-threaded + compaction** for write-heavy proof work — the discovery-brief (cross-session distilled map) + `--auto-reset` (sheds bloated sessions) *are* the compaction layer. Per Cognition / MAST / Kim et al., this beats multi-agent decomposition for tightly-coupled coding.
2. **Subagents as read-only offloaders only** — delegate lemma-search / multi-file reads / Z3-dump digestion to the built-in `Agent` tool (which already isolates), and apply the returned summary yourself. Never delegate writing. (`prompt.md` recast to this framing is queued.)
3. **Do not rebuild the writing-coordinator.** It was redundant (subagents already isolate) and the real gap is behavioral — forcing delegation would need separate `run.py`-level prover processes, the only enforcement that survives `Bash cat` / tool-restriction propagation.

---

## E3. CHECKLIST.md multi-module campaign state

### Pain it addresses

Running multiple targets across a layer (or across all 10 layers) currently requires shell scripting around `run.py` and piecing together results from scattered JSON files. No single view of "what's proven, what's deferred, what's in-flight."

Numina's CHECKLIST.md solves this: one Markdown file per campaign, with structured entries per target (status, attempts, last error, informal-proof version, tmp file). Coordinator agent reads/writes it between rounds.

### Trigger

Add when you're running ≥5 targets in sequence and post-run analysis requires opening more than 3 result directories to understand state. Concretely: when you find yourself writing shell one-liners like `jq '.success' results/*/result.json | sort | uniq -c` regularly.

### Design sketch

```markdown
# Campaign: <run_id>
Generated: 2026-04-22 14:30:15

## Summary
- Total: 12
- Done: 7  In Progress: 2  Todo: 2  Blocked: 1

### [L0-001] field_specs.rs — field_add_spec
- Status: ✅ done
- Attempts: 1
- Verified: 2026-04-22 14:35
- Proof file: results/.../round_1.json

### [L0-002] field_specs.rs — field_mul_spec
- Status: 🔄 in_progress
- Attempts: 2
- Last error: "unresolved import vstd::math::pow"
- Failure memory: 2 entries (see failure_memory.json)

### [L0-003] primality_specs.rs — axiom_p_is_prime
- Status: ⏭️ skipped (axiom module)
...

### [L9-012] scalar_ops.rs — scalar_sub_borrow
- Status: ❌ blocked (exceeded 5 rounds)
- Next step: needs E4 informal-prover
```

### Integration points

- New `lib/checklist.py` — parse/write CHECKLIST.md (~150 LOC). Parser is strict markdown; writer uses a template.
- `run.py` gains `--campaign <path>`: reads CHECKLIST to pick next target, updates status after run.
- `scripts/checklist_stats.py` — summarize any CHECKLIST.md (replaces the shell one-liners).
- Only written by the harness (or, in autoproof mode, the coordinator). Never the LLM alone — prevents the EXP-007 class of bugs where self-reported success is fake.

### Cost

~250 LOC Python. Low friction once you want campaign-level tracking.

---

## E4. Informal-prover skill

### Pain it addresses

Some proofs fail not because the LLM lacks lemmas but because it **lacks a mathematical strategy**. E.g., a function whose correctness depends on a non-obvious invariant the LLM keeps failing to discover. No amount of syntactic search helps — you need a human-style proof outline first.

Numina's Informal Prover (`prompts/docs/prompts/informal_agent.md` in Numina) fills this: a Gemini/GPT loop that generates an NL proof, verifies it 3× against itself (score 0 / 0.5 / 1), refines on feedback, returns a verified NL outline. That outline then guides the Lean formal attempt.

Paper evidence (Table 2): adding the informal prover to Numina took Putnam 2025 from **4/12 to 11/12**. Single biggest ablation delta.

### Trigger

Add when you observe ≥3 modules where the LLM exhausts attempts at L0/L1 without Verus errors that suggest missing lemmas — the errors are "failed to verify" with unclear proof-state failures. Indicates strategy gap, not lookup gap.

### Design sketch

`skills/informal_prover.py` (Numina-faithful port):

```
Input: function signature + requires/ensures + optional Rust body
Process:
  for attempt in 1..K:
    gen_prompt = "Prove this carefully, atomic steps, no hand-waving ... {problem}"
    solution = call_gemini(gen_prompt) or call_gpt(gen_prompt)
    verify_prompt = "Score this solution 0/0.5/1 ... {problem} {solution}"
    verification = call_verifier(verify_prompt)  # run 3 times; accept only if all 3 say 1
    if score == 1: return solution
    refine_prompt = "Refine the solution based on feedback ... {problem} {solution} {verification}"
    solution = call_llm(refine_prompt)
  return best_solution_so_far
Output: markdown proof outline, saved to results/<run_id>/<target>/informal.md
```

### Integration points

- Call site is the **prompt** — when at L2 (or when MVP fails and you have E1 installed), the coordinator prompt invokes `informal_prover.py` to produce `informal.md`, then passes that path to the next L0 attempt.
- Gemini and/or GPT API key in env (`GEMINI_API_KEY`, `OPENAI_API_KEY`). Optional deps in `pyproject.toml`.
- 20-iteration cap (Numina's value); 3× verification check (Numina's value).
- Adds a real cost channel: each informal call is ~10-50¢ of Gemini.

### Cost

~300 LOC Python (most of it prompt templates). Two new optional deps.

---

## E5. Full spec-integrity tracker with auto-restore

### Pain it addresses

The MVP has `spec_check` as a **gate** — it detects drift and fails the round. That's sufficient for single-function runs. But for multi-function / multi-round runs, a mid-session drift in round 3 blows away work from round 1, and the agent has to learn the lesson again.

Numina's statement tracker goes further: detects drift AND **auto-restores** the original signatures before the next round. The agent is protected from itself — the restoration is silent, transparent, and logged as a `[warn]`. Also distinguishes allowed changes (added new statements) from forbidden changes (modified/removed existing).

### Trigger

Add when `spec_check` gates are firing often (say, >1 per 10 rounds) — it means the agent keeps trying to weaken specs and a hard fail every time is wasting budget. Or when you're running campaigns (E3) where one target's spec drift shouldn't tank the whole campaign.

### Design sketch

Extend existing `skills/spec_check.py`:

```python
# New entry points:
spec_check.py --snapshot <file> --out <snapshot.json>     # (MVP already has)
spec_check.py --verify <file> --against <snapshot.json>   # (MVP already has) — gate mode
spec_check.py --restore <file> --to <snapshot.json>       # NEW: rewrite file to original sigs
spec_check.py --diff <file> --against <snapshot.json> --category {modified,added,removed}  # NEW
```

Classification (per Numina):
- **modified** — existing `fn foo(...) ensures P { ... }` where `P` changed → FORBIDDEN, restore
- **removed** — signature gone entirely → FORBIDDEN, restore
- **added** — new `fn bar(...)` appears that didn't exist before → ALLOWED (might be a helper lemma the agent legitimately introduced)

Logic for restore: AST-level (use `syn` via a small Python wrapper, or a targeted brace-matcher with regex). Preserves agent's body edits; only restores the `fn` header + `requires` + `ensures` + `decreases`.

### Integration points

- `run.py` calls `spec_check --restore` between rounds when drift detected (instead of failing the task).
- `round_N.json` gains `spec_restored: bool` and `spec_restore_details: [...]`.
- The LLM is told in the next round's prompt: "Note: your edits to function X's signature were reverted. Prove it as specified, do not modify the spec."

### Cost

~150 LOC on top of the MVP gate version. Key risk: the restore primitive must be correct — an incorrect restore could corrupt a file mid-run. Needs careful tests, ideally a dry-run mode (`--restore --dry-run`) that emits a patch without applying.

---

## E6. Spec-inference experiment mode (spec-proof / proof-only)

**STATUS: built on the `spec_gen` branch** (`run.py --experiment-mode`), not in `main`. Listed here so the *pattern* — varying *what the task is*, not just how hard the agent tries — has a documented home and trigger, the way the other five do.

### Pain it addresses

The MVP fixes the specs and asks the agent to fill `admit()`s. But a large part of the real cost of Verus-izing a codebase is writing the fn-header specs (`requires` / `ensures` / `decreases`) in the first place. The MVP had no way to exercise or measure the agent on *spec reconstruction* as a capability distinct from proof construction.

### Trigger

Add when you want to evaluate or improve spec-writing separately from proof-writing — e.g. a benchmark that strips fn-header specs from a known-good module and measures whether the agent can re-derive them so a fixed higher-level anchor still verifies.

### Design sketch

Two modes selected by `--experiment-mode` (requires `--experiment-allow-edit` to name the dep files the agent may edit):

- `spec-proof` — dep fns have their fn-header specs stripped; the agent infers them (guided by `skills/infer_verus_spec/`) so a fixed anchor verifies.
- `proof-only` — specs are fixed; the agent only adds proof scaffolding to dep bodies seeded with `admit()`.

`strip_specs.py` (a top-level init/harness tool, sibling of `run.py` / `admit.py` — not an agent skill) produces the stripped inputs; `build_experiment_block` in `run.py` injects the mode-specific prompt addendum. The spec-drift gate still protects the *anchor*; the `--experiment-allow-edit` files are exempt by design (that is the whole point of the mode).

### Cost

Already built: ~235 LOC in `run.py` (experiment block + allow-edit gate plumbing) + the `infer_verus_spec` doc skill + `strip_specs.py` (~456 LOC). The new axis is the cost — it widens "what success means" beyond the MVP's single `verus_okay` + zero-admit definition, so eval comparisons across modes are not apples-to-apples.

**Rungs built since (the difficulty ladder grew past spec-proof / proof-only).**
`--experiment-mode` now has four values, each a `demo_decompress.sh` rung
(documented in [website_backend.md](website_backend.md)): `proof-only`,
`spec-proof`, `contract-only` (anchor's proof body + helper lemmas stripped, its
contract frozen), `bridge-specs` (the two shared Montgomery↔Edwards `open spec
fn`s deleted; whole-crate verify + a frozen-file guard pin the reconstruction),
and `bridge-full` (the whole decompress proof tree cut below the curve layer —
across `decompress_lemmas.rs` and `curve_equation_lemmas.rs` — as a **pure
proof-reconstruction** task). EVERY spec definition is frozen, including the map
(`montgomery_to_edwards_affine`), so the agent never reconstructs a definition
and the user-facing contract is *structurally* un-weakenable. The 10 decompress-
path lemmas are all **deleted outright** (`--delete-fn`); the agent re-derives
each (signature + contract + proof). Deleting rather than keeping their contracts
is contract-safe because the API ensures reference only frozen spec fns and the
editable files hold zero spec fns — so no re-derived lemma contract can weaken
the guarantee (a too-weak one just fails the frozen proof → not COMPLETE). Field
arithmetic / `vstd` and the contract vocabulary stay frozen. The list is a fixed
property of the pinned source, so `demo_decompress.sh` names it explicitly (no
scan). (`bridge-specs` remains the dedicated map-reconstruction rung — it *does*
reconstruct the map, pinned by the frozen `to_edwards` proof.)
The bridge rungs are the first to require **whole-crate** verification each round
(the pins live in other modules) and a **frozen-file guard** (`FROZEN_EDIT`); both
treat the two modes identically (`run._BRIDGE_MODES`), differing only in how much
is stripped and the prompt addendum.

**Build-side reconciliation (peel, `spec_gen`).** The init-states for all these
modes — previously hard-coded as bespoke `strip_specs.py`/`admit.py` invocations
in `demo_decompress.sh` / `launch_specgen.sh` — are now expressed as **peel
manifests** (`peel_manifests/*.json`) and built by `peel.py`, a single
data-driven init-state builder. It collapses the strip/delete/admit verbs onto
one **peel-depth** axis (P1 proofs → P2 lemmas → P3 specs → P4 contract,
cumulative + totally ordered) with a per-file `proof_op` and an enforced **pin
rule** (`peel._require_pin` refuses a P4 contract-strip or a P3 spec-delete
without a declared `proof`/`consumer:NAME`/`oracle:REF` pin — the same soundness
discipline the curated rungs got from freezing). It *composes* the existing
transforms rather than reimplementing them, so it adds little new surface
(`peel.py` + `peel_run.sh` + `tests/test_peel*.py`); the peel builder leaves
`run.py` untouched (the separate spec-definition gate adds one flag,
`--check-spec-defs`, to run.py's verify call). The
run-side mode is **declared in the manifest, never inferred from depth** — depth
is a monotone "strip shells 1..k" axis and structurally cannot express "strip
stratum k, keep 1..k-1 as the pin" (the `bridge-specs` shape). `peel_run.sh`
chains `peel.py --worktree` → `run.py`, reading `experiment_mode` +
`editable_files` out of peel's JSON. See `docs/spec_gen_runbook.md` §1 and
`peel_manifests/README.md`. (The two gate-OFF `spec-proof` rungs and the
directory-cut `--strip-to-fields` rung are covered there too — the latter via
`peel.py --classify`, which generates that manifest.)

---

## E7. False-contract classification (verified-counterexample escalation)

### Pain it addresses

In any mode where the agent *reconstructs a contract* (field-floor / bridge / the
`spec-proof` rungs), it can invent a `requires`/`ensures` that is **too weak to be
true**, stub the body with `admit()`, and move on. The admit-counter then reports
it as "1 remaining admit" — visually identical to a genuinely-hard-but-true
obligation. On a contract-frozen resume the false contract is pinned, so the goal
is *unclosable*, not merely hard. **"k admits remaining" silently conflates two
populations** (hard-but-true vs false-contract), so the headline difficulty metric
is wrong, and a run can grind rounds on an obligation no proof can discharge.
Full writeup of the failure mode: the CryptoProver paper (false-contract discussion).

### Trigger

**FIRED** — corefloor_006 + its resumes. 006 reached 8 hard admits; resumes could
not close them and the agent escalated `NEEDS_DECOMP`. Two of the eight were
verified *false* by hand (`lemma_on_curve_x_zero_implies_y_pm_one`: `x=0, y=p+1`
satisfies `requires` but breaks `ensures`; `lemma_carry8_bound`: `carry8=2^53+13`
likewise) — 006-authored too-weak contracts, not the intended spec. The manual
counterexample check is exactly what should be a harness primitive.

### Design sketch

Classification **gated by a machine-verified witness** — NOT agent-asserted, and
NOT a general satisfiability solver. The agent already produces the witness during
reasoning, so the harness only has to verify it. **Only the witness comes from the
agent; the contract comes from the harness's own frozen snapshot.**

1. **Escalation evidence schema.** Extend the escalation (a new
   `END_REASON:FALSE_CONTRACT`) to require, per blocked obligation:
   ```json
   {"function": "...", "file": "...",
    "witness": {"<var>": "<concrete value>", ...},
    "why_requires_holds": "...", "why_ensures_fails": "..."}
   ```
   **Note the `requires`/`ensures` text is NOT in the schema.** Letting the agent
   quote the predicate reopens the very escape vector this closes: it could quote a
   *strawman weakened* `ensures`, verify the strawman is false, and bail on a
   hard-but-true proof. The predicate must come from the frozen source of truth.
2. **Witness checker (the only "detector" worth building).** A harness-private
   CLI — `skills/check_false_contract.py` — that looks up `(file, function)` in
   the **`spec_check` snapshot** (already taken before the loop; holds each fn's
   frozen `requires`/`ensures`), and builds the goal
   `requires(witness) && !ensures(witness)` from *that* contract substituted at
   the agent's witness — emitted as a `proof fn` inside the fn's own module so
   the frozen spec vocabulary is in scope — then runs `cargo verus` (reuse
   `verus_check`). The agent does **not** call this checker and does **not**
   supply proof hints; it writes witness data only, and the harness verifies.
   - **Verifies ⇒ contract provably false** → classify `FALSE_CONTRACT`. Sound: no
     false positives.
   - **Does not verify ⇒ UNCONFIRMED, not "hard-but-true."** Verus/Z3 is
     sound-but-incomplete (nonlinear arith, missing triggers, timeout can leave a
     *true* `requires∧¬ensures` goal undischarged), so a rejected witness only means
     "not machine-confirmed false" — the obligation might still be false with a
     witness Z3 can't crunch. The action is the same (reject the escalation, keep
     grinding — errs toward more work), but the label must be `unconfirmed`, so the
     `false_contracts` count is honestly a **lower bound**.
   This closes the escape vector — "cry false-contract to bail" is the same
   incentive class as a fake green, so it's verified-against-the-frozen-contract,
   not trusted (the §1 gate philosophy).
   - **Witness expressibility (bound):** each witness value must be a
     Verus-expressible closed value to substitute (the two fired examples —
     `p+1`, `2^53+13` — are). Values are inserted into generated `let`
     bindings, so the checker rejects statement separators, braces, comments,
     unbalanced delimiters, and proof-bypass tokens (`assume`, `admit`,
     `external_body`, etc.) before injection. A witness only definable via an
     existential or an opaque spec-fn output can't be mechanically substituted,
     so such cases stay `unconfirmed`.
   - **Positive proof evidence:** the checker injects a deliberate tripwire
     (`assert(false)`) beside the witness proof and only accepts
     `FALSE_CONTRACT` when the tripwire reports an error and the witness proof
     does not. This distinguishes "proved" from "Verus never reached the
     injected region because of an earlier build abort."
3. **Surface it.** `result.json` / `failure_memory.json` / the admit inventory
   gain `false_contracts: [...]` (verified) and `unconfirmed_false: [...]`, so
   "8 hard admits" renders as "2 verified-false, N unconfirmed, the rest hard".
   `run._final_end_reason` treats `FALSE_CONTRACT` as terminal-and-non-promotable
   (parity with the drift labels); `unconfirmed` does not change the run outcome.

### Integration points

- New harness-private `skills/check_false_contract.py` ((file, function,
  witness) → look up the frozen contract in the `spec_check` snapshot → sanitize
  witness values → Verus goal + tripwire → `cargo verus`).
- `run.py`: parse the witness evidence from the escalation, call the checker
  (which sources the predicate from the snapshot, not the escalation), set
  `end_reason`/inventory fields; pin the label in `_final_end_reason` +
  `tests/test_admits.py`.
- Prompt: when the agent believes a frozen contract is false, it must escalate
  with the witness evidence rather than leaving a bare `admit()`.

### Cost

~150 LOC + tests. Risk is low: the checker only *verifies* a supplied witness
against the **frozen** contract, reusing existing infra; it never tries to
*decide* arbitrary specs. The asymmetry is deliberate — a verifying witness proves
the contract false (sound, no false positives), while a non-verifying one is
reported `unconfirmed` (Verus is sound-but-incomplete), so the verified count is a
lower bound. Does NOT repair contracts — repair is the agent's job in a fresh
reconstruct run, or a different cut that freezes the *original* contracts (see §6
option 1). This feature only *measures/classifies*; it does not hand-craft proofs.

---

## E8. Scope decomposition (per-file / per-cluster runs)

### Pain it addresses

The whole-crate experiment modes (`field-floor` / `bridge-*`) hand **one agent the
entire editable file set and the entire whole-crate error cone at once**. On a large
cut that cone is hundreds of errors, and a single agent staring at it plateaus and
bails — the failure is **capacity, not knowledge** (the agent often knows the right
idiom; it can't hold ~350 simultaneous obligations + their context in one session).
This is distinct from E1 (which decomposes a *single hard proof* into sub-lemmas)
and E2 (which isolates *context* via subagents but still points one agent at the
whole cone). E8 decomposes the **editable file set across sequential runs** so each
agent faces a tractable error count.

### Trigger

**FIRED** — observed live on 2026-06-27:

- `trustedcore_resume_007` (VM2): **rounds 1-4 all `LIMIT`** while the whole-crate
  error count churned in the low hundreds (`353 → 345 → 346 → 345`) — real but
  insufficient movement — *despite* local module checks on the same worktree being
  an order of magnitude smaller and going green (`backend::serial::u64::field`
  `28 → 27`, `load8_lemmas` `2 → 0`). The same work is tractable at module scope
  and intractable at whole-crate scope.
- Prior `corefloor` runs 003/004/005 plateaued ~370-379 with a single
  whole-crate agent (per the T70/T115 ledger record).
- *Early corroborating signal* (not yet a fired ≥3-LIMIT datapoint):
  `peel_corefloor_006_gcp13` (VM1) opened at whole-crate `errors=147` with a
  module check at `errors=4` and no `round_1.json` / `END_REASON` boundary yet —
  the same scale gap, but too early to count as a plateau.

The signature: a whole-crate run that stays `LIMIT` for ≥3 rounds with the
whole-crate error count stuck in the low hundreds (churning, not necessarily
flat), *while* the per-module checks in the same `cli.log` are an order of
magnitude smaller and individually closable.

### Design sketch

A **build/launch-side cluster scheduler** (mirrors how `peel.py` sits on the build
side and leaves `run.py` untouched). It does **not** add a cascade inside `run.py`.

```
1. Partition the --experiment-allow-edit list into CLUSTERS, dependency-ordered
   by PEEL-DEPTH (lower layers first: common_lemmas -> field -> edwards ->
   ristretto), so a cluster's proofs can rely on lemmas an earlier cluster
   already proved. When editable `specs/*` files are in scope they MUST be
   ordered before their consumer clusters (a consumer proof needs the spec
   defs/bridge lemmas first). Error-locality is only a residual second-pass
   tie-breaker, not the primary ordering. Reuse peel.py --classify for the cut.
2. Run the agent once PER CLUSTER, sequentially on the one worktree, each sub-run
   scoped to edit only that cluster's files (a subset --experiment-allow-edit).
   Each agent now faces ~tens of errors, not ~hundreds.
3. After all clusters: one authoritative WHOLE-CRATE verify gates COMPLETE.
   Per-cluster greens are PROGRESS, not completion.
4. If the whole-crate pass still has errors (cross-cluster breakage), re-cluster
   the residual errors and run a bounded second pass (cap the number of passes).
```

**Soundness invariant (the load-bearing rule):** COMPLETE is gated on the final
**whole-crate** verify, never on a disjunction of per-cluster greens. A per-cluster
green only means "tractable locally"; cross-cluster breakage (cluster B's proof
relied on a cluster-A lemma that later changed) is caught by the whole-crate gate.
This keeps the `verus_okay ∧ zero-hard-admit` COMPLETE definition intact — the
decomposition changes *how the agent gets there*, not *what counts as done*.

### Integration points

- New `cluster_run.sh` / `lib/cluster.py` — partition + dependency-order the
  allow-edit list, then drive N sequential `run.py` invocations (subset
  `--experiment-allow-edit` each) on one worktree + one `--results` root, each
  with a **unique cluster run-id / task dir** (so the per-`target_id`-keyed
  `failure_memory` / `discovery_brief` don't collide across clusters).
- Reuse `peel.py --classify` for the partition; reuse the existing whole-crate
  verify (`verus_check --whole-crate`, already the bridge-mode gate) for step 3.
- **Carryover is NOT free — the scheduler must make it work** (codex, T115):
  - **Catalog cache:** the prompt tells agents to *reuse, never rebuild*
    `catalog_cache.json` (`prompt.md:14`), so cluster B's `search_*` will **not**
    find the helper lemmas cluster A just added unless the scheduler
    **invalidates/rebuilds the catalog cache** (or uses a cluster-local cache)
    after each source-changing cluster.
  - **ProvenRegistry:** `proven_registry.json` only appends on `success ==
    COMPLETE` (`run.py:5235`, `5320-5333`). In E8 a cluster can make real local
    progress yet end `LIMIT` (the final whole-crate gate is red from *other*
    clusters), so its new lemmas are **not** recorded. Do not rely on
    ProvenRegistry for partial cluster progress unless the scheduler writes a
    separate, explicitly non-COMPLETE "cluster-proven lemmas" artifact.
  - **failure_memory / discovery_brief** are keyed by `target_id` (`run.py:5290`,
    `5307-5315`), so carryover only works if the scheduler picks stable per-cluster
    ids (see the run-id requirement above).
- `run.py` itself is **unchanged** — same one-loop, same gates. The scheduler is
  external, exactly like `peel_run.sh`.

### Cost

~200-300 LOC for the scheduler + clustering (most of it partition/ordering logic;
the run-driving is a loop over existing `run.py`). No new agent skills.

### Caveat (why this needs a spec entry, not just a script)

It **changes the unit of evaluation**: a "run" is no longer one agent over the whole
cut but N agents over partitions, so per-cluster metrics are not comparable to a
single whole-crate run, and total agent wall-clock multiplies (each round is
cheaper, but there are more of them across clusters). Report both per-cluster and
whole-crate outcomes, and only ever headline the whole-crate gate. This crosses the
MVP "no batch runner / no cross-round escalation" non-goal line — it is a
deliberate `spec_gen` addition with the trigger above, recorded here so the drift is
documented, not silent.

### Status note (relation to the gap taxonomy)

In the knowledge / capacity / incentive framing (ledger T115): E8 is the **capacity**
lever, E6's `{METHODOLOGY_BLOCK}` + the failure-memory ladder are the **knowledge**
lever, and the spec/axiom/tooling/forbidden-construct gates are the **incentive**
lever. They are complementary by construction — decomposition makes each run small
enough that methodology conditioning can actually bite, which is why E8 is the
prerequisite structural change before more prompt tuning earns its keep on
whole-crate cuts.

---

## Priority if you add them

If forced to rank, based on expected ROI from inference-dalek's post-mortem data:

1. **E5 full spec tracker** — cheap and defensive; inference-dalek's EXP-001/003 `external_body` bypass is exactly this pain
2. **E3 CHECKLIST** — multiplier for debugging / campaign management
3. **E1 multi-level cascade** — unlocks the off-target / large-body cases (biggest single-level blocker)
4. **E4 informal prover** — biggest potential lift (paper: 4/12 → 11/12) but only for a specific pain pattern
5. **E2 subagents** — highest complexity, smallest expected delta (paper: 11/12 → 12/12 — one problem), only needed for context-degradation at scale

But again — **don't add any until the MVP exhibits the symptom they solve**. Inference-dalek's FEATURES_ANALYSIS is the cautionary tale.
