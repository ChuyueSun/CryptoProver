# Redundant racing — first-valid-wins across N cold attempts (implementation handoff)

**Audience:** an agent (or me) picking this up cold.
**Branch:** `spec_gen_subagent_context`. **Repo:** `~/dalek-lite-mvp`.
**Status:** spec only — nothing built yet.

---

## 0. Why this exists — read first

This is the successor to the carryover thread (v1/P1/P2), and it comes from
admitting what that thread proved: **the problem is variance, not context.**

**What the carryover thread actually established (see `docs/extension_spec.md` E2):**
- Bloat is not promptable; carrying state into a fresh session neither curbs
  bloat nor improves outcomes (v1 reverted, P1 null, P2 one-harm-no-benefit).
- The dominant *measured* phenomenon across the P2 A/B was **per-attempt
  variance**, not bloat-blocking-completion:
  - `c1` **failed** (NEEDS_DECOMP) while `c2` **succeeded** — *same arm, same
    target, same flags.*
  - `b1` cost \$2.25 vs `b2` \$6.06 — identical 1-round wins, 3× spread.
  - Whether a run found the cached proof (`search_proven`) was luck of the draw.
- Nothing in that A/B failed *because of bloat*. Targets completed despite it.

**What `~/autoform-bot/docs/harness-design-lessons.md` says to do about variance
(Lesson 4 — "race redundant agents, don't partition"):**

> For open-ended/uncertain tasks (proving a lemma), splitting work perfectly is
> impossible — you don't know the solution shape in advance. **Redundancy with
> first-past-the-post converts variance into wins.** Cancellation reclaims
> compute the moment a winner lands.

The carryover thread tried to make **one** session more reliable. Lesson 4 says:
don't — run **N independent cold attempts** on the same target, **first to pass
the existing final gate wins, cancel the rest.** Applied to our data: racing 3
fresh `window.rs` attempts makes `c1`'s poisoned failure a non-event — the
fastest winner (b1 at ~6 min, or whichever racer ports the cached proof) defines
the run outcome.

This also rides machinery we already have and exercised this session:
worktree isolation (autoform Lesson 3, used for the P2 A/B) and
`proven_registry` / `search_proven` (a racer that stumbles onto a cached proof
wins fast — reuse-luck compounds with redundancy).

**The honest trigger status (anti-speculation discipline, per the repo ethos).**
Racing crosses the MVP non-goal "no batch runner / no harness-coordinated
parallelism," and it costs up to N× compute per target. It is justified *more*
than the Stage 2 planner/prover split (whose trigger — bloat blocking completion
— is still unproven) because the variance it targets is **measured**. But it is
still a spec-not-build until §4's validation shows racing N beats single-shot on
**success-rate and/or wall-clock** on a variance-heavy target. No claim without
that A/B — same rule that produced the P2 doc.

---

## 1. What to implement — `race_task`

A thin orchestrator that runs `K` independent `run_task` attempts on **one**
target, each in its **own git worktree + own results subdir**, and returns the
first attempt whose `TaskResult.success` is true (the existing final gate:
`verus_okay AND 0 hard admits`). Losers are killed the moment a winner lands.

It is **ordinary async Python** (autoform Lessons 1–2: "a subagent is just an
`Agent` + `.call()`; orchestrate with plain async"). No dispatch protocol, no
inter-process result assembly — that is the whole point versus Stage 2.

### 1.1 Placement & shape

New file `race.py` (sibling of `run.py` / `run_layer.py`), or a `run_race.py`.
Do **not** bloat `run_task` — racing wraps it, never edits its body.

```
race_task(target, project, K, base_commit, worktree_base, results_root,
          per_attempt_kwargs) -> RaceResult
  1. Resolve K worktrees of `project` at `base_commit` (reuse the §3 recipe).
  2. Launch K attempts concurrently, each:
       - own worktree (its own target path inside it),
       - own results subdir results_root/<run_id>/attempt_<i>/,
       - cold start (NO carryover — racing replaces carryover, see §0),
       - all other run_task kwargs identical.
  3. First attempt with success=True → WINNER. Record its worktree + diff.
  4. Cancel/kill every other attempt's process group immediately.
  5. Merge the winner's proof back to the canonical project (or just report
     its worktree path + diff for a human/caller to apply).
  6. Write ONE proven_registry entry for the winner (serialized — §2.3).
  7. Tear down all K worktrees.
RaceResult = winner TaskResult + per-attempt summaries + wall-clock + total cost.
```

### 1.2 Process model — subprocess, not in-process threads

Each attempt must be a **separate `run.py` subprocess** (via `launch.sh` or a
direct `python3 run.py` with `start_new_session=True`), NOT `run_task` called in
K threads of one interpreter. Reasons:
- `run.py` already puts each `claude` in its own process group and installs a
  killpg lifecycle (`_LIVE_PROC`, the SIGTERM handler) — **cancellation of a
  loser = `os.killpg` on that attempt's group.** In-process threading would
  share `_LIVE_PROC` (a module global) and the signal handlers, and you could
  not cleanly kill one racer's claude tree without touching the others.
- Subprocess isolation also protects against one attempt crashing the
  orchestrator.

So `race.py` is an `asyncio` loop over K `asyncio.create_subprocess_exec`
handles; "first valid wins" = poll each attempt's `result.json` (or exit code)
as it finishes, and on the first `success=True`, `killpg` the rest.

---

## 2. The sharp decisions (get these right)

### 2.1 Win condition = the EXISTING final gate, nothing new
A winner is an attempt whose `result.json` has `success == True`, which already
means `verus_okay AND hard_remaining == 0` (run.py's `done_for_real`). Do **not**
invent a new acceptance check — reuse the gate that already guards COMPLETE, so
racing can never accept something a single run wouldn't. A `NEEDS_DECOMP` or
`LIMIT` attempt is **not** a winner; if *all* K attempts finish non-success,
`race_task` returns the "best" loser (most admits filled) + an aggregate
end_reason, and the run is a genuine failure (no false green from racing).

### 2.2 Cancellation must be prompt and total
The moment a winner lands, `killpg(SIGKILL)` every other attempt's group — claude
+ cargo verus + z3 + Monitor loops. This is the autoform Lesson 4 payoff
("cancellation reclaims compute the moment a winner lands"); without it, racing
costs a flat K× instead of "K× until first win." The loser worktrees are then
removed. Guard against the SIGKILL race where a loser writes `result.json` after
you've decided the winner — the winner is whoever you committed to first.

### 2.3 Serialize the ONE shared writable resource: `proven_registry.json`
(autoform Lesson 5: "find the single point of contention and put the lock
*there*; everything else stays parallel.") `run.py` appends to
`results_root/proven_registry.json` with an **unlocked read-modify-write** — and
CLAUDE.md already documents that this race is *why `launch.sh` is sequential*.
Two options:
- **Preferred:** give each attempt its **own `--results` subdir** (so each writes
  a *private* registry), and have `race_task` write the **single canonical
  registry entry for the winner only**, under a `fcntl.flock` on the canonical
  `proven_registry.json`. Stdlib `fcntl` — no new deps. (Mirror autoform's
  `_flock_nonblocking`: poll `LOCK_NB` + sleep, never block uninterruptibly.)
- Failure_memory.json has the same race; same fix (per-attempt private, merge at
  the end). Don't let K attempts share one registry/memory file.

### 2.4 Worktree lifecycle (reuse the P2 A/B recipe — §3)
K worktrees at `base_commit`, created **serially** (autoform Lesson 5 again:
concurrent `git worktree add` corrupts git state — serialize *creation*, then run
parallel). Always `--force` remove on teardown, including on exception/Ctrl-C
(wrap in try/finally). The winner's proof lives in its worktree; either merge it
to the canonical project before teardown or copy out its target file + `diff.md`.

### 2.5 Cold attempts only — racing REPLACES carryover
Each attempt is a vanilla cold `run_task` (`--no-reset-carryover`, no analyzer).
Racing is the recovery mechanism now; do not stack it on the reverted carryover.
Per-attempt auto-reset (cold) stays on — an individual racer may still bloat and
cold-reset internally; that's fine and orthogonal.

### 2.6 Bound the cost (autoform Lesson 8: every loop has a ceiling)
- `K` is small (3–5; autoform races a handful, not dozens).
- Each attempt keeps its own `--max-task-minutes`; `race_task` also takes a
  global wall-clock cap after which all attempts are killed and the best loser
  returned.
- Optional early-stop: if `j < K` attempts have already hit `NEEDS_DECOMP`
  naming the *same* missing infrastructure, that's signal the target genuinely
  needs decomposition — consider stopping early rather than burning all K.

---

## 3. Worktree recipe (lifted from the P2 A/B, validated this session)

```bash
# serial creation
for i in $(seq 1 K); do
  git -C <project> worktree add --detach /private/tmp/race-<rid>-$i <base_commit>
done
# parallel run: K × (launch.sh --detach … --project /private/tmp/race-<rid>-$i/<crate> \
#                       --results results-race-<rid>/attempt_$i --no-reset-carryover <target>)
# teardown (always): git worktree remove --force /private/tmp/race-<rid>-$i
```
`launch.sh --detach` is REQUIRED from the Bash tool (CLAUDE.md). `base_commit` is
the all-admits state (the P2 A/B used `c60b2682`).

---

## 4. Validation A/B — REQUIRED before any claim

Same discipline as P2: race vs single-shot, controlled, let it decide.

- **Arms:** `S` = single cold `run_task` (K=1, the status quo); `R3` = race K=3;
  (optional `R5` = K=5).
- **Target:** a **variance-heavy** one where single-shot success is
  *probabilistic* — i.e. where single runs sometimes fail/sometimes pass. The P2
  data suggests window.rs is borderline-easy (5/6 passed); pick something with a
  real failure rate: a Layer-B/C module, or `sqrt_ratio_lemmas.rs`.
- **REMOVE the cached proof first** (`results/larger_example_002/...window.rs`
  and any prior proven copy of the chosen target) so attempts must prove
  manually — otherwise you re-measure cache-port luck, not racing (this was the
  P2 confound; do not repeat it).
- **Reps:** ≥3 per arm (variance is the whole point; n must be ≥3 to estimate a
  success *rate*).

**Metrics (the headline is success-rate, not Δadmits):**
- **P(success) per arm** — does R3 finish targets that S sometimes fails? This is
  the Lesson-4 thesis.
- **wall-clock to first win** (R3 should be ≈ the *fastest* of its 3, not the
  mean) vs S's mean.
- **total cost** — R3 pays for losers until cancellation; is `P(success)` lift
  worth the multiple? Report cost-per-success, not raw cost.

**Decision rule (write the verdict into `docs/extension_spec.md`, new E-section):**
- **R3 P(success) > S and cost-per-success acceptable** → racing earns its keep;
  it becomes the primary recovery lever; wire it into `run_layer`.
- **R3 ≈ S on P(success)** → the target wasn't variance-limited (single-shot
  already reliable); racing is wasted compute *here* — note it and pick a harder
  target, or conclude variance isn't the bottleneck after all.
- **R3 wall-clock ≫ fastest-of-3** → cancellation isn't reclaiming compute;
  fix the kill path before judging.

One run decides nothing — same lesson that produced the P2 doc.

---

## 5. Risks / things not to re-learn

- **K× cost if cancellation is weak.** The entire economy of racing is prompt
  total cancellation (§2.2). Measure wall-clock-to-first-win vs fastest-of-K to
  confirm it's working.
- **Registry/memory write races (§2.3).** The reason launch.sh is sequential.
  Per-attempt private files + a single flocked winner-write, or you corrupt
  cumulative state.
- **False greens.** Reuse the existing gate (§2.1). Racing must not lower the
  acceptance bar — it only runs more *attempts* at the same bar.
- **Don't race on a non-variance target.** If single-shot already passes
  reliably, racing is pure waste; the A/B must use a target with a real failure
  rate, or it measures nothing (and looks like a null for the wrong reason).
- **Don't stack racing on carryover.** Cold attempts only (§2.5). Carryover is
  reverted; racing replaces it.

## 6. Definition of done
1. `race.py` (`race_task` + flocked winner-registry-write + worktree
   create/teardown + killpg cancellation), subprocess-per-attempt.
2. The §4 A/B (≥3 reps, S vs R3, cache removed, variance-heavy target),
   metrics tabulated — **headline P(success), report cost-per-success**.
3. A verdict paragraph in `docs/extension_spec.md` with the keep/revert decision
   and the numbers — **no claim without the A/B.**
