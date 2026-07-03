# Stage-3 certificate run — proof-shape evolution log (final)

> Tracks how the agent's proof architecture **emerges over time** in the
> field-floor certificate run (`field_floor_stage3_096_480_fable`, H0,
> launched 2026-07-02 18:04Z) — including what it got wrong and fixed, for
> the paper's evolution narrative. Sources: `claude_raw/round_N.jsonl` (full
> edit history + agent narration), `cli.log` (verifier timeline), the
> AGENT_DEBATE.md ledger (codex's raw-stream watch), span-aware admit
> inventories. Maintained live during the campaign; now final (sealed 2026-07-03).
> Mining methodology caveat: "lemma redefined Nx" counts any Edit/Write whose
> new content contains the definition — whole-file rewrites inflate it;
> semantic revisions are confirmed by contract-text diff or narration.

## Timeline (all times Z, 2026-07-02)

| t | event |
|---|---|
| 18:04 | Launch. Baseline **red-130** (= census: 124 frozen-callsite name pins + 5 editable + wrapper) reproduced on first verifier call |
| 18:04–18:50 | **Phase 1 — pin-guided scaffolding**: signatures + contracts + `admit()` bodies for name-pinned lemmas, working from frozen-caller E0425s. Files chosen in pin order (curve_models→double/niels; lizard→coset; scalar_mul backend→pippenger/straus/vartime; u64/scalar→montgomery chain) |
| ~18:49 | **M1 compile-green** (~45 min; predicted 1–2 h). Module greens are admit-masked scaffolds; hard-admit count becomes the metric (peak ≈ 59) |
| 18:50–19:48 | **Phase 2 — bottom-up discharge**: admits 59→51→44→42→37; `vartime_double_base` 5→0, `straus` repaired + 7 discharged, scalar-byte homes both genuinely green (20:00, 20:08) |
| 19:32/19:43 | First whole-crate checks: **166 errors = edwards 59 + scalar 52 + ristretto 30 + montgomery 22** (+ noise) — within 1 each of the four Class A API baselines (60/53/31/23): the endgame surface equals the sum of the calibrated API rungs |
| 19:48 | Round 1 ends (round-level LIMIT, correct); round 2 resumes with harness feedback |
| 20:10– | Round 2 drilling `montgomery_reduce_part1_chain` local red-1 with full Verus output; `lemma_as_bytes_52` under active revision |

Inventory at 20:17Z: **23 non-axiom admits** (niels 5, pippenger 4, straus 4,
curve_equation 4, montgomery_reduce 3, coset 1, mul_base 1, double 1);
**88 distinct lemmas authored** so far vs GT's 235 deleted — a much leaner
inventory at this stage. Diff +3272/−22 across 13 files; 0 forbidden
constructs; 0 untracked files; `shift_lemmas.rs` (trusted substrate)
untouched — call-only.

## Wrong → fixed (the paper's correction catalog)

1. **Too-strong contract, self-diagnosed and weakened** (r1, ~19:00): agent
   narration verbatim — *"My contract was too strong — `from_montgomery`
   only has `limbs_bounded`. The value < 2^260 = R < R·L, so `limbs_bounded`
   suffices. Weakening."* The inverse of the too-weak failure mode the
   prompt warns about: caller-driven contract calibration operating in both
   directions.
2. **Straus assertion failure → explicit-witness repair** (19:0x): repeated
   `straus_lemmas` red-2 on `u64_5_as_nat(id.Y.limbs) == 1`; fixed by adding
   explicit limb asserts + `lemma_mul_basics` — the classic
   "SMT needs the limb decomposition spelled out" pattern, then 7 admits
   discharged in the same home.
3. **The PROBE experiment** (19:24–19:27, self-cleaned): agent inserted
   `probe_construct_identity_edwards` with literal comment *"PROBE: ghost
   construction of EdwardsPoint outside its module"* to test whether it
   *could* ghost-construct the type outside its module — verified the probe
   green, learned the answer, **removed the probe itself** before it could
   contaminate the tree. Deliberate hypothesis-testing behavior worth a
   sentence in the paper.
4. **Edit-conflict recovery** (19:08): one transient `File has been modified
   since read` tool failure; agent recovered via Bash rewrite without losing
   the thread.
5. **Coordinated group-law revision wave** (r1 #1164–#1182): the Edwards
   group-law lemma family (add_identity_left/right, add_commutative,
   scalar_mul_identity/additive/succ, z_one_affine…) re-edited as a batch
   mid-round — the agent revisiting its own foundational layer once consumers
   exposed requirements. (Semantic-diff extraction pending; file-rewrite
   inflation possible for some members.)

## Interesting observations (running list)

- **Trust-surface parity**: the agent's Montgomery proofs route through the
  same pre-existing `assume`-backed u128 shift helpers in
  `common_lemmas/shift_lemmas.rs` that GT's own proofs call 9× — found the
  identical path through the trusted floor without seeing GT (ruled
  GT-parity; trusted-surface usage diff added to the seal battery).
- **Endgame forecast from structure**: the whole-crate error surface after
  lemma scaffolding ≈ exactly the sum of the four Class A API-rung
  baselines — evidence the cut decomposes the way the calibration assumed.
- **Leanness so far**: 88 authored lemmas vs 235 GT at the equivalent
  coverage point mirrors the 092 finding (agent ≈ half GT's proof mass) at
  cone scale — final ratio to be measured at seal.
- **Pin-guided file order**: first-hour edit order tracked the frozen-caller
  pin distribution almost exactly (biggest pin clusters first), i.e. the
  environment's structure, not any prompt hint, determined the build order.

## Attempt 1 final chapter (sealed LIMIT 02:10Z, 4 rounds / 8.1 h)

**Whole-crate descent**: 166 (first archived in-run measurement; preflight
compile-class census 130) → 154 (r2) → 113
(r3) → **69** (seal). Lemma layer fully de-stubbed by mid-r3 (non-axiom
admits 59-peak → 0); three of four API modules green — montgomery.rs
23:31:11 (r3), then decompress/elligator support lemmas, then ristretto.rs
at 02:00:43 (r4, six minutes before deadline). scalar.rs partially proven;
**edwards.rs never reached** — the remaining 69 ≈ the un-started edwards
layer (Class A 094 baseline: 60).

**Had-wrong-and-fixed (r2–r4 additions to the r1 list)**:
- `lemma_as_bytes_52` contract revised again in r2 (third revision — byte
  canonicalization threshold).
- The part1-chain red-1 that closed r2/opened r3: an off-by-one-limb bound
  in the reconstructed montgomery carry chain; fixed after the agent
  requested the full Verus output through round feedback (the designed
  drill-down path).
- Ristretto endgame oscillation (31→20→3→15→9→6→4→2→0): the red-15 spike
  was a helper-name qualification error (two unqualified paths in the new
  `sum_of_slice`/`Sum` proof) — a compile-class regression from a proof
  edit, immediately diagnosed and qualified.

**Integrity (verified at seal)**: zero gate events across 8.1 h; crate-wide
non-axiom 0 / axiom exactly 48; untracked 0; external_body byte-stable;
21/21 modified files editable; frozen-file rlimit attrs = pre-existing base
content; agent-added rlimit(20) ×2 (montgomery.rs:444 — the same
resource-marginal fn as Class A/GT — and ristretto.rs:2243). The PROBE
lemma (labeled exploration) was self-removed. No unearned COMPLETE claim.

**Attempt 2 (097) seed continuity**: relaunched 03:14Z from a fresh peel +
`--seed-wip` patch (launcher guard: exactly 21 editable files) +
attempt-1's failure memory. First in-run whole-crate measurement: **69 —
identical to attempt-1's final state**. The evolution record is continuous
across the attempt boundary; attempt-1's tree is archived untouched
(stage3_evidence_backups/096_final_031656Z, 30 MB incl. all raw streams).

## Obligation-hardness ranking (by module and VC class; snapshot 05:37Z, attempt 2 in flight)

**By module** (hardness = wall-time + rounds + budget-attr pressure, both attempts):

1. **ristretto batch-compress assembly** — the single hardest spot in the
   cone for invented architecture. Sequence observed: monolithic
   `lemma_batch_compress_step` at `rlimit(600)` → rejected by the SMT
   resource frontier even at 60× budget → agent decomposes ("single-query
   body is too big") → two sublemmas *still* at the frontier →
   `rlimit(900)` experiment in flight. GT pre-paid this exact cost with a
   23-lemma fine-grained home (Class A 091: +1256 lines, 55 min, attr-free).
2. **scalar `non_adjacent_form`** — the sole load-bearing beyond-GT budget
   in the API layer (`rlimit(150)`; codex's stripped-copy gate isolated
   it). NAF bit-manipulation + windowing arithmetic. The scalar family also
   produced attempt-1's r2/r3 drill-down (part1-chain carry off-by-one).
3. **edwards decompress/step1 foundations** — not solver-hard but
   architecture-hard: attempt 2 spent its first ~1.5 h building four
   support homes (+450 lines) before edwards.rs would fall; largest
   invented-infrastructure investment per API green.
4. **montgomery `mul_bits_be` ladder** — resource-marginal for *every*
   prover that has touched it: GT's own in-source comment, Class A 092's
   `rlimit(20)`, attempt-1's `rlimit(20)` — three independent instances;
   the margin case — though note (disclosed 2026-07-03): GT's in-source
   rlimit-fragility comment survived the cut and was visible to Class B
   agents, so their attr placements there may be comment-guided rather than
   independently convergent.
5. **Trait/trivia impls** (eq, ct_eq, conditional_select, identity,
   zeroize) — free for every prover, zero proof text needed.

**By VC class** (hardest first):

1. **Nonlinear field-arithmetic assembly** (mod-p product chains: xDBL/xADD
   algebra, sqrt-ratio, batch-invert telescoping) — every rlimit hotspot in
   both attempts is in this class. Hardness signature = SMT resource
   exhaustion, not logical gaps: the proofs are *found* but not *afforded*
   without decomposition or budget.
2. **Multi-lemma postcondition bridges** (API `ensures` needing invented
   intermediate contracts; baseline postcond counts: scalar 28, edwards 13)
   — where the lemma-invention phase concentrated; hard by architecture
   search, not solver cost.
3. **Type-invariant construction** (edwards "constructed value may fail to
   meet its declared type invariant" ×4) — motivated attempt-1's PROBE
   experiment (ghost-construction semantics) before being closed.
4. **Loop invariants** (ladder + batch loops; ~10/file baseline) —
   moderate; closed structurally in all attempts, no budget attrs.
5. **Bounds-plumbing preconditions** (`fe51_limbs_bounded` chains; edwards
   31, ristretto 17 baseline) — highest *count*, lowest difficulty:
   mechanical weaken/sum-lemma pattern discharges them.
6. **Termination/decreases** — zero incidents campaign-wide.

**The Class B hardness thesis these support**: difficulty concentrates
where *solver cost meets invented decomposition*. Class A never saw this
frontier because GT's decomposition pre-paid it; Class B's agent pays it
live — first with budget, then (when the frontier doesn't move) by
rediscovering fine-grained splitting. The resource frontier, not logical
reach, is the operative hardness measure for whole-cone reconstruction.

## THE CERTIFICATE: attempt 2 (097) sealed COMPLETE — 2026-07-03 06:34Z

**Whole-cone reconstruction converged.** Attempt 2 (seeded from attempt 1's
69-error state + its own failure memory): 3.3 h, one harness round, ending
whole-crate **0 errors at default rlimit**, spec gate clean, confirmation
gate green, sealed COMPLETE. Campaign totals for Class B: 2 attempts, 11.4 h
agent time, first-measured 166 verification errors → 0.

**Attempt-2 evolution highlights**:
- Seed continuity exact: first in-run whole-crate measurement = 69,
  attempt-1's final state to the error.
- Edwards (the untouched file): ~1.5 h of foundation invention across four
  support homes (+450 lines) before the API fell — green 04:21Z.
- Scalar: strict-default red-9 → red-2 structurally, then two
  `rlimit(150)` attrs + one assertion fix → green 04:38Z
  (`non_adjacent_form` later confirmed the only load-bearing one).
- **The batch-compress saga** (hardness ranking #1, resolved): monolithic
  `lemma_batch_compress_step` at `rlimit(600)` → check timeout; `rlimit(900)`
  experiment → two 900 s verifier timeouts ("budget cannot buy this proof");
  agent narrates "single-query body is too big" → structural extraction
  (`lemma_ed0_closed_form` + sublemma split, attrs reduced to 300) → two
  reassociation fixes → green 06:04Z. *Decomposition beat budget on the
  agent's own initiative — the solver taught it GT's proof-engineering
  lesson without hints.*
- Whole-crate red-6 interlude: the crate gate caught staleness in three
  foundation homes re-edited during the edwards push (module checks had
  never re-covered them) — repaired in ~6 min. The whole-crate gate doing
  exactly its job.

**Final cone metrics**:
- GT proof infrastructure removed by the cut: **22,753 lines**; agent's
  verified replacement: **11,024 lines (48.5%)** (git numstat, 26 editable
  files, base vs sealed worktree; dual-verified) — replicating the 092
  case-study ratio (47%) at cone scale. **196 authored lemmas vs GT's 235
  (83%; 88 same-home shared names / 108 inventions / 147 GT lemmas never
  needed)**.
- Budget attrs (stratified per the 00:55 ruling; per-attr classification
  COMPLETE): within-GT-envelope 2×`rlimit(20)`; beyond-GT 3×`rlimit(150)` +
  4×`rlimit(300)` (the 600/900 experiments did not survive into the seal).
  Targeted single-attr-removal gates show **exactly one is load-bearing**:
  `scalar::non_adjacent_form` (rlimit 150, 15× default) — the other six
  (as_radix_2w, step1, all four batch-compress) are droppable under their
  module gates. The certificate's true beyond-GT budget footprint is one
  function.
- **Second-machine cross-check**: untouched-copy whole-crate on VM2 (all
  attrs intact): okay=true, 0 errors, 2031 verified — identical verdict to
  VM1's independent container. Certificate marker:
  `corefloor-plus-fieldfloor-certificate-097` @ `06160297`, parent 095's
  marker, tree `177914d4` == base (empty diff verified on both refs by
  executor and verifier).

**Certificate battery (all green)**: harness seal (0 errors / 0 hard admits
/ 0 drift); daemon inventory (0 non-axiom, exactly 48 axiom; 0 untracked;
external_body byte-stable); independent fresh-container ro-mount
whole-crate at default rlimit **0 errors / 2031 verified**;
erasure-equivalence audit passed on all four API files (zero base exec
lines removed across both attempts; all additions ghost); every modified
file inside the editable 26 (launcher seed-guard + git audits concur); no
PROBE artifacts; sterility verified at both launches. Zero gate trips and
zero fake greens — through the end of the campaign.

*(Certificate evidence: stage3_evidence_backups/097_certificate_064133Z +
096_final_031656Z on VM1 — full raw streams both attempts, final tree
patches, independent verify JSONs.)*
