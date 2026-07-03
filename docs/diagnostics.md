# Diagnosing failure patterns from logs

When a run produces unexpected results, **don't trust the summary** —
walk the artifacts in this order to find the actual cause:

```
results/<run_id>/<task_id>/
├── result.json              ← end_reason, success, rounds_used
├── round_N.json             ← per-round verus_okay, verus_errors[], spec_drift, end_reason
├── claude_raw/round_N.jsonl ← agent's full reasoning + tool_use stream
├── cli.log                  ← which skills the agent invoked, in order
└── prompt_rendered.md       ← exact prompt the agent received
results/failure_memory.json  ← cumulative; fed back into next run's prompt
```

`result.json` says "what". `round_N.json` says "why per round". The
`claude_raw/*.jsonl` says "what the agent thought". Always read at least
the first two before jumping to a hypothesis.

**Ground-truth source.** When comparing the agent's output against a
"correct" proof, **always pull from a canonical git ref**, never the
local working tree of `~/dalek-lite/curve25519-dalek/`. Agent runs can
leave that worktree contaminated with edits from prior failed attempts,
and treating the contaminated state as ground truth produces wrong
diagnoses. Use:

```bash
git -C ~/dalek-lite/curve25519-dalek show \
  <commit-or-HEAD>:curve25519-dalek/src/<path>.rs > /tmp/gt.rs
```

The reference commit for this benchmark is **`ChuyueSun/dalek-lite`**
(a fork of `Beneficial-AI-Foundation/dalek-lite`), pinned at
**`d74d68927edf7d08b1e4c977550eeb381fe15326`** (main @ 2026-03-23) — the
experiment starting states are constructed from this fork, so its proof
source is the authoritative "correct" answer to diff against. Prefer this
exact commit over a moving `HEAD` (or use a specific commit the user
supplies). When pulling, point `-C` at a checkout of the fork:

```bash
git -C <fork-checkout> show \
  d74d6892:curve25519-dalek/src/<path>.rs > /tmp/gt.rs
```

**Canonical-style notes (the proof guidance the GT repo ships).** Beyond
the proof *source*, the repo ships the notes its own proof/spec agents
follow, under `.codex/skills/verus-proof-helper/references/` (mirrored at
`.claude/skills/verus-proof-helper/`). These are general, shared guidance —
identical in upstream `Beneficial-AI-Foundation/dalek-lite` and the
`ChuyueSun` fork — so you can read them from either; for an exact match to
the experiment starting state, read them from the pinned fork commit:

- `lemma-reference.md` — where the reusable lemmas live and where new ones
  belong (`lemmas/common_lemmas/{to_nat,pow,div_mod,bit}_lemmas.rs`, then
  domain folders `field_lemmas/`, `edwards_lemmas/`, …) plus canonical lemma
  names (`lemma_pow2_adds`, `lemma_mod_mod`, `lemma_u8_32_as_nat_*`, …).
- `techniques.md` — the canonical tactic menu (`bit_vector`,
  `nonlinear_arith`, decomposition, `calc!`, induction-with-`decreases`,
  loop invariants, compositional postcondition reasoning).
- `common-issues.md` — error→fix table (mode mismatches, array-interp
  errors, quantifiers not instantiating, `rlimit`/timeouts, ghost-import
  guards). Its **#1 rlimit mitigation is to scope lemma calls inside
  `assert(...) by { ... }` blocks** so each lemma's facts don't pollute the
  global SMT context.
- `patterns.md` / `workflow.md` / `quality-bar.md` — worked compress/decompress
  proofs, the "move `assume(false)` one step at a time" development loop, and
  the cleanup bar (`axiom_` = admitted, `lemma_` = fully proved; wrap every
  lemma call in an `assert(fact) by { lemma(); }`).

When a diagnosis hinges on "is the agent's *approach* canonical?" (patterns
3, 6 below), check the agent's proof against these notes, not just against
the proof source. Pull them the same way as the source:

```bash
git -C <fork-checkout> show \
  d74d6892:.codex/skills/verus-proof-helper/references/common-issues.md
# or, without a checkout, from the upstream guidance:
gh api repos/Beneficial-AI-Foundation/dalek-lite/contents/.codex/skills/verus-proof-helper/references/common-issues.md \
  --jq '.content' | base64 -d
```

---

## Pattern catalog

Each entry: **symptom you'd notice**, **detection command**, **root
cause**, **fix**.

### 1. Fake-green run — agent never authenticated

**Symptom**: every module passes in suspiciously short, near-uniform time
(~30–60s); summary says "verified=N/N"; cumulative tokens reported as 0.

**Detect**:
```bash
# Token usage zero across the entire run? almost certainly auth-broken.
jq '.round_results[].claude_usage' results/<run>/<task>/result.json
# Confirm by inspecting the stream-json:
#   - terminal result event has is_error:true with api_error_status:401
#     (apiKeySource:"none" alone is NOT a fake-green signal — that's normal
#      for OAuth-via-keychain auth even on successful runs)
#   - any assistant messages produced after a 401 use model:"<synthetic>"
jq -c 'select(.type=="system" and .subtype=="init") | .apiKeySource' \
  results/<run>/<task>/claude_raw/round_1.jsonl
jq -c 'select(.type=="result") | {is_error, api_error_status, duration_ms}' \
  results/<run>/<task>/claude_raw/round_1.jsonl
jq -c 'select(.type=="assistant") | .message.model' \
  results/<run>/<task>/claude_raw/round_1.jsonl | head -1
```

**Cause**: `claude -p` got HTTP 401 (token expired, conflict between
keychain OAuth and `ANTHROPIC_API_KEY`, or desktop subprocess can't
reach the host's IPC credentials). Each round exits in ~800ms with no
edits. `cargo verus verify` then runs against the unedited file — if
the file already has 0 admits, verus passes trivially and the harness
records a fake COMPLETE.

**Fix**: run `claude -p "say hi"` outside the harness; if it 401s, do
`claude /logout && claude` and complete the OAuth flow in a non-desktop
terminal. Verify the keychain credential's `expiresAt` is in the future:
```bash
security find-generic-password -s "Claude Code-credentials" -a $USER -w \
 | python3 -c 'import json,sys,datetime; o=json.load(sys.stdin)["claudeAiOauth"]; print(datetime.datetime.fromtimestamp(o["expiresAt"]/1000))'
```

### 2. Baseline contamination — agent fills its admit, file still LIMITs

**Symptom**: agent's reasoning text says "errors are pre-existing in
`lemma_X`, not in my change"; current admit count = 0 but
`verus_okay=False` and `LIMIT`.

**Detect**:
```bash
# Errors are at line ranges OUTSIDE the function the agent edited.
jq '.verus_errors[] | {file, line, data}' results/<run>/<task>/round_5.json
# Compare to ground truth — does main have these proofs working?
git -C <ground-truth-repo> show main:<src/path> | grep -c 'admit()'
```

**Cause**: the worktree's baseline already had pre-broken proofs in
non-admit functions. The agent's "stay in scope" instinct is correct,
but the harness's `verus_okay AND zero_admits` gate doesn't model
"baseline was broken before I started." Often coincides with rlimit
failures (pattern 3).

**Fix**: pre-clean the baseline so non-admit code already verifies; or
treat these as benchmark noise, not synthesis failures.

### 3. Z3 rlimit on nonlinear arithmetic

> For the *positive* version of this — how the canonical GT proofs decompose
> nonlinear goals instead of hammering them — see
> [gt_proof_style.md §3](gt_proof_style.md).

**Symptom**: same N errors across all 5 rounds, no progression in
`admits_remaining`; error data contains
`"assert_nonlinear_by: Resource limit (rlimit) exceeded"`.

**Detect**:
```bash
jq '.verus_errors[].data' results/<run>/<task>/round_5.json \
  | grep -i 'rlimit\|nonlinear'
# How does the agent currently approach the proof?
grep -c 'broadcast use\|by (nonlinear_arith)\|lemma_mul_is_' \
  <worktree>/<src/path>
```

**Cause**: agent attempts bulk nonlinear reasoning — typically via
`by (nonlinear_arith)` blocks or excessive `lemma_mul_is_commutative`
calls. On 5×5+ polynomial expansions this exhausts Z3's rlimit. The
canonical proof in this codebase (verified against the GT source for
`lemma_u64_5_as_nat_product` at the pinned commit above) does **not**
use `broadcast use` groups or a single bulk `nonlinear_arith` block over
the full product. Instead it delegates nonlinear work to pre-proven
helpers in `crate::lemmas::common_lemmas::mul_lemmas` —
`lemma_mul_distributive_{3..8}_terms`, `lemma_mul_w0_and_reorder`,
`lemma_mul_si_vi_and_reorder` — and uses `lemma_mul_is_distributive_add`
as the workhorse for combining cross-product sums.

Note the nuance: this is **not** a blanket ban on `by (nonlinear_arith)`.
The GT `techniques.md` notes (see "Canonical-style notes" above) endorse
`nonlinear_arith` for *small* multiplication/division inequalities and
transitivity chains; what blows the rlimit is throwing the *whole* 5×5
expansion at one `nonlinear_arith`/`assert_nonlinear_by` block instead of
decomposing it through the `mul_lemmas` helpers.

**Two failure shapes observed on the same root cause** (verified on
`u64_5_as_nat_lemmas` across v3/v4/v5 reruns):
(a) early — admits still in file, Z3 hits per-assert rlimit on
`assert_nonlinear_by` blocks (this Pattern 3).
(b) later — agent has filled admits but the proof is bloated with
redundant calls (e.g. 10× `lemma_mul_is_commutative` and ~20×
`lemma_pow2_adds` vs the canonical 0 and 13); cumulative Z3 sub-queries
exhaust the 300s wall-clock cap (Pattern 6). Same fix applies.

**Fix**: confirm the failure_memory hint (rendered in
`prompt_rendered.md` after recent rlimit failures) recommends the
`common_lemmas/mul_lemmas` helper family. If absent, your
`last_verus_err` capture might be missing `messages[].data` — search
for the `last_verus_err = ` assignment inside `run_task` in
[run.py](../run.py). Also avoid suggesting `broadcast use` for this
codebase — verification against the ground truth shows that's not the
canonical pattern here, even though it's a common Verus idiom in
general. Per the GT `common-issues.md` notes, the first-line rlimit
mitigation is to **scope each lemma call inside its own `assert(...) by
{ ... }` block** so its facts don't pollute the global SMT context — a
cheaper fix to try before reaching for the `mul_lemmas` decomposition.

### 4. Axiom-by-convention LIMIT

**Symptom**: file has 1 admit, agent runs all 5 rounds quickly (~30s
each), no edits made, declares LIMIT every round. File names like
`*_specs.rs` (e.g. `edwards_specs`, `window_specs`).

**Detect**:
```bash
# Is the admit inside an axiom_* function? If yes, the agent is right
# to refuse — these are axioms-by-convention.
awk 'BEGIN{a=0} /^[[:space:]]*((pub|broadcast|open|closed)[[:space:]]+)*proof[[:space:]]+fn[[:space:]]+axiom_/{a=1;next} a&&/^}/{a=0;next} !a&&/admit\(\)/{print FILENAME":"NR}' <target>
```

**Cause**: hardcoded-data axioms (e.g. `axiom_ed25519_basepoint_table_valid`)
cannot be discharged by SMT and are intentionally left as `admit()`.
The prompt forbids `external_body` and `assume`, leaving the agent no
honest path. Output is empty → no LLM-target admits remain.

**Fix**: `_count_llm_target_admits` in [lib/admits.py](../lib/admits.py) excludes
`proof fn axiom_*` bodies, so these now COMPLETE correctly. If a
similar pattern appears under a different naming convention, extend
the regex in `_AXIOM_FN_NAME_RE`.

### 5. Premature COMPLETE — agent missed an admit

**Symptom**: `rounds_used=1`, very short duration (<1 min), agent's
last line is `END_REASON:COMPLETE`, but file still has admits.

**Detect**:
```bash
jq '{end_reason, rounds_used, duration_seconds}' results/<run>/<task>/result.json
# Was there a 'continuing' diagnostic from the harness?
grep 'claimed COMPLETE but' <log>
```

**Cause**: the agent's self-COMPLETE check is "ran `verus_check`,
got `okay:true`" — but `admit()` trivializes any postcondition, so
verus passes even with admits remaining. Agent doesn't grep for
remaining admits before declaring done.

**Fix**: harness loop break only fires when `verus_okay AND
_count_llm_target_admits==0`; otherwise prints a diagnostic and
continues. Subsequent rounds carry failure_memory feedback that
reminds the agent it missed something.

### 6. Verus 300s timeout (whole-file too complex)

**Symptom**: `verus_errors[0].data` says
`"verus timed out after 300s and was killed"`; `admits_remaining=0`
but `verus_okay=False`.

**Detect**:
```bash
jq '.verus_errors[0].data' results/<run>/<task>/round_5.json | grep -i 'timed out'
```

**Cause**: agent removed all admits but its proofs are large/complex
enough that whole-file verification (cargo + z3 + rust_verify) exceeds
the 300s wrapper timeout. Different from rlimit — Z3 budget didn't
exhaust per-assert; the file as a whole is too big.

**Fix**: same canonical solution as Pattern 3 — delegate nonlinear work
to the pre-proven helpers in `crate::lemmas::common_lemmas::mul_lemmas`
(`lemma_mul_distributive_{3..8}_terms`, `lemma_mul_is_distributive_add`).
The bloat-induced wall-clock timeout has the same root cause as the
rlimit case: too many redundant Z3 sub-queries from manual identity
enumeration. The `failure_memory.py` hint now fires for both patterns.

### 6a. Whole-crate cut stops at target-local green

**Symptom**: in a `field-floor` / `bridge-*` whole-crate run, the target
module verifies and the agent says some form of "target complete", but
whole-crate errors remain in editable non-target files. The round ends
`LIMIT` with little or no proof work in the broader cone.

**Detect**:
```bash
# Did the agent anchor on the target instead of the editable list?
python3 replay.py results/<run>/<task>/claude_raw/round_N.jsonl --full \
  | grep -Ei 'target .*complete|assigned target|broader cone|non-target'

# Did it actually edit the broader editable files?
python3 replay.py results/<run>/<task>/claude_raw/round_N.jsonl --only tool_use \
  | grep -E 'Edit|MultiEdit|Write'

# Was the harness still correctly refusing COMPLETE?
jq '{end_reason, verus_okay, spec_drift, admits_remaining}' \
  results/<run>/<task>/round_N.json
```

**Cause**: pre-`7715f11` prompts rendered the experiment-mode block first
("editable list is the assignment") and then later generic text that said
"work/edit/read only the target file". In gcp4 round 1 this conflict
made the agent treat `ristretto.rs` as done and leave the real
whole-crate error cone untouched.

**Fix**: use a harness at `7715f11` or newer. Whole-crate modes now
render the target path as a harness anchor and the
`--experiment-allow-edit` list as the actual assignment, admit-count
scope, and workflow scope. For older results, treat this as a prompt
scope bug, not as evidence the broader cone was impossible.

### 6b. Claude retry exhaustion with no result event

**Symptom**: a round has a `claude_raw/round_N.jsonl` stream consisting
only of `system` `api_retry` events, no final `type:"result"` line, and
little or no corresponding `round_N.json` / `result.json`. Older
harnesses can make this look like a no-op proof stall.

**Detect**:
```bash
RAW=results/<run>/<task>/claude_raw/round_N.jsonl

# No result event at all?
jq -c 'select(.type=="result")' "$RAW"

# Retry budget exhausted on null/unknown transport errors?
jq -c 'select(.type=="system" and .subtype=="api_retry")
       | {attempt, max_retries, error_status, error}' "$RAW"
```

**Cause**: `run_claude_round` used to parse only the final `result`
event. If Claude exited nonzero after exhausting non-429 retries, the
empty result dict skipped the `RATE_LIMITED` fast-path; the return code
was stored but not used in the decision path, so the unchanged file fell
through to normal spec/verus checks.

**Fix**: `7715f11` classifies positive nonzero no-result exits before
the spec/verus gates. A trailing `api_retry` stream becomes
`RETRY_EXHAUSTED`; other positive no-result exits become
`TRANSPORT_ERROR`. Both are visible, non-promotable infrastructure
outcomes, not proof LIMITs. Negative signal exits (for example a
deadline SIGKILL) stay on the existing timeout/deadline path.

### 7. Spec drift

**Symptom**: end_reason `SPEC_DRIFT`, loop terminated early.

**Detect**:
```bash
# Drift count > 0 means at least one spec mutated this round.
jq '.spec_drift | length' results/<run>/<task>/round_N.json
# Drilldown: which functions changed?
jq '.spec_drift' results/<run>/<task>/round_N.json
```

**Cause**: agent modified a `fn` header / `requires` / `ensures` /
`decreases`. The hard-fail gate exists because weakening specs is the
cheapest way to make verus pass — don't relax this.

**Fix**: usually agent compliance. If the agent insists on touching
specs, the prompt isn't strict enough; emphasize Rule 1 in `prompt.md`.

### 8. Stale catalog cache

**Symptom**: edits to `lib/catalog.py` (e.g. broadcast group sig
enrichment) don't appear in `search_*` results.

**Detect**:
```bash
# Look for the new text in cached entries
jq -r '.entries[] | select(.name|startswith("group_")) | .signature' \
  <results-dir>/catalog_cache.json | head -3
```

**Cause**: catalog cache fingerprint is based on source-tree mtimes
only — editing `catalog.py` doesn't invalidate it.

**Fix**: `rm <results-dir>/catalog_cache.json` to force rebuild on
next run. Or extend the fingerprint to incorporate a `_CATALOG_VERSION`
constant that you bump on schema changes.

### 9. Zombie / orphan workers (parallel runs)

**Symptom**: process count higher than expected; worker writing to a
file you didn't intend it to touch (different `--run-id` than the
current run).

**Detect**:
```bash
pgrep -lf "run.py.*--run-id"           # all workers + their run-ids
ps -ax -o pid,ppid,command | grep run.py | grep -v grep
```

**Cause**: a prior `kill -TERM` against the parent shell didn't
propagate to children; the orphaned bash script kept iterating its
task list and spawning new run.py instances.

**Fix**: kill the actual script PID (`pkill -TERM -f rerun_failed.sh`),
not just the wrapping zsh. After kill, `pgrep -lf "run-id"` should
show only the workers you intended.

### 10. Cargo lock contention (parallel only)

**Symptom**: `cargo verus verify` invocations take 2–3× longer when
multiple workers run on the same project; per-worker `cli.log` shows
long gaps between `verus_check INFO` start and `verus_check INFO
result`.

**Detect**:
```bash
grep -E 'verus_check INFO (cmd|result)' results/<run>/<task>/cli.log \
  | head -20
```

**Cause**: cargo serializes builds via `.cargo-lock` on the same
project root. Parallel speedup applies to agent reasoning, not verus
checks.

**Fix**: accept ~1.5–2× speedup instead of N× for N workers. For true
parallelism, give each worker its own cargo project copy (`git worktree
add --detach <path> eval/admitted-start`) **and** its own `--results` dir
— the worktree clears `.cargo-lock`, the separate `--results` clears the
`failure_memory.json` / `proven_registry.json` / `catalog_cache.json`
read-modify-write race. See "Creating a clean admitted worktree" in
README.md / CLAUDE.md / AGENTS.md for the full recipe.

---

## Triage flowchart

When a module fails (`success=false`), check in order:

1. **Authentication** — was the agent actually running? Check token
   usage and the init message's `apiKeySource`. If broken, fix auth
   and re-run; everything else is downstream.
2. **Did the agent edit anything?** Check `tool_use` distribution in
   `claude_raw/round_*.jsonl`. Zero `Edit` calls + only
   `verus_check`/`spec_check` = pattern 1, 4, or auth issue.
3. **Did the file change?** `git diff <baseline> <worktree>/<file>`.
   If unchanged, agent gave up (pattern 4); if changed but admits
   remain, see patterns 5/7.
4. **What does verus actually say?** `jq '.verus_errors[].data'`.
   If rlimit → 3; if "timed out" → 6; if compile error → upstream
   issue, possibly pattern 2.
5. **Cross-reference with `failure_memory.json`.** Repeated patterns
   across runs are the trigger for adding a tactical hint to
   `as_prompt_block`.

---

## When you don't recognize the pattern

Add a new entry above. The minimum bar:

- a 1-sentence symptom that's checkable from logs
- one `jq`/`grep` command that confirms it
- a guess at the root cause
- a fix or escalation path

If a pattern fires across ≥3 runs, consider promoting it to an automated
hint inside `lib/failure_memory.py::as_prompt_block` so the next agent
run sees the recommendation in its prompt.
