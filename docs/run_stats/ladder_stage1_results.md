# Field-floor convergence ladder — the complete Class A record (lemma ladder + API rungs, 2026-07-01 → 07-02)

> **CLAIM SCOPE — read first.** The ladder rungs below are **NOT the
> field-floor experiment**. Each rung froze every *other* file **as proven
> ground truth**: GT callsites dictated the lemma inventory, GT proofs of
> neighboring homes were readable in the worktree, and GT's decomposition was
> imposed rather than invented. These banks are **feasibility/engineering
> results** (per-slice reconstructibility given GT-shaped surroundings, plus
> harness validation). The field-floor claim proper — reconstruct the whole
> cone pinned **only by the API contracts + frozen specs + trusted floor**,
> with no GT proof code visible (surviving GT comments disclosed below) — is
> tested **only** by the Stage-3
> monolithic run (full `field_floor.json` cut, all homes + API bodies cut
> simultaneously, fresh results root, no ladder artifacts readable). Bank
> marker refs are empty-diff vs `corefloor-base`, so that experiment remains
> uncorrupted by the ladder.

Campaign record of the reachable-slice **ladder** that took the field-floor
*campaign* from "no lane converges" to a verified de-stubbed reconstruction of
the **entire scalar foundation in a single run**, plus the Edwards/Ristretto
(Stage-2) rungs. Primary evidence: `AGENT_DEBATE.md` ledger (joint
claude-relaunch/codex verification trail); per-run artifacts on VM1/VM2 under
`agents_scalar_slices/` / `agents_edwards_stage2/`; bank refs in the
`gt_scratch_1114160` clone.

## Experimental setup (explicit, for the paper)

### Common infrastructure (all experiments)

- **Subject**: dalek-lite (`curve25519-dalek` Verus port), Cargo workspace
  member; proven tip **`corefloor-base-103b92b9`** (whole-crate
  `cargo verus verify -p curve25519-dalek`: 2066 verified / 0 errors).
- **Harness (run image)**: this repo at commit chain `b1113fb` → `a56f284`
  (prompt lane-neutrality fix), baked into docker image
  `dalek-harness:a56f284-fieldfloor-warm` — built by `git archive` of the
  commit only (never a dirty tree), cargo target pre-warmed, then the proven
  warm source **deleted** (`rm -rf /opt/warm-src`); `/opt/harness` read-only
  to the run UID; container mounts = its own `work/` + `results/` only.
  Verified per launch: in-container `run.py` sha == `a56f284`;
  `/opt/warm-src` absent; mount list.
- **Host-side peel toolchain (builds init states; NOT in the run image)**:
  `peel.py` + `strip_specs.py` copies on the VMs, advanced mid-campaign to
  `c1b4aac` (orphan-doc-comment fix in `strip_specs.py`, committed + resynced
  to both VMs 2026-07-02). The fix affects lemma-**deletion** cuts only:
  lemma rungs built before it may retain orphaned doc comments (cosmetic;
  init states semantically unchanged); API strip-all cuts delete nothing and
  are unaffected. The Stage-3 manifest build (which deletes all 235 lemmas)
  uses the fixed tool.
- **Agent**: headless `claude -p`, model `claude-fable-5` (exception: run 069
  on the default model — the model-effect datapoint), ≤8 rounds; wall budget
  120–240 min per lemma rung, 180–360 min per API rung (Stage-3 certificate:
  360–480 planned); one isolated peeled git worktree per run.
- **Trusted base (never editable, in every experiment)**: external `vstd`;
  the L4 field-lemma layer; the L5 number-theory/`common_lemmas` substrate;
  **every `spec fn` definition** (`specs/*`; bodies frozen by
  `spec_check --check-spec-defs`); backend exec code; all exec code; all 48
  `proof fn axiom_*` (axioms-by-convention).
- **Integrity gates (detection, all runs)**: `SPEC_DRIFT` (any change to an
  existing fn header, `requires`/`ensures`/`decreases`, or spec-fn body),
  `AXIOM_DRIFT` (new `axiom_*`), `FORBIDDEN_CONSTRUCT` (new `assume(...)` or
  `#[verifier::external_body]`), `TOOLING_DRIFT` (harness skills/lib hash),
  `FROZEN_EDIT` (any edit outside the declared editable set),
  `GIT_RECOVERY` (recovering pre-cut sources from git history) — all
  **non-promotable** (a tripped gate can never be promoted to COMPLETE).
  SPEC/AXIOM/TOOLING drift are terminal for the run; `FROZEN_EDIT` is
  recoverable by reverting the frozen edit (observed in run 077 round 3:
  contaminated green caught → reverted → clean round-4 seal).
- **Bank criterion (scoring)**: harness seal `end_reason=COMPLETE` requiring
  whole-crate **0 errors at default SMT rlimit** (never `--rlimit 80`, never
  module-scoped) **and** crate-wide **non-axiom `admit()` count == 0**
  (span-aware counter over every `.rs`) **and** no gate tripped —
  independently re-verified after seal (fresh admit inventory on the final
  worktree, forbidden-construct scan, cli.log gate-command audit).
- **Bank recording**: each bank = a **no-delete marker ref** (empty diff vs
  `corefloor-base`) — a validation record, *not* a source transplant; the
  agent-written proofs live only in the run's sealed worktree/artifacts. The
  base ref used by later experiments is therefore byte-identical to the
  original proven tip.

### Experiment class A — ladder feasibility rungs (runs 069–091, 073, 077, and the API rungs 092+)

- **Init state**: worktree at `corefloor-base`; `peel.py` deletes exactly the
  rung's named lemmas (signature+contract+body) from 1–8 editable files — or,
  for API rungs, strips all inline `proof{}` blocks from one API file with
  headers/contracts/exec kept byte-identical. **Every other file is
  byte-identical ground truth, present and readable.**
- **Pins**: frozen spec definitions + frozen contracts crate-wide (as above)
  **plus the frozen GT proofs of every non-editable file** — the frozen
  consumers *call* the deleted lemmas and their proofs *use* the reconstructed
  `ensures`, so a vacuous/too-weak reconstruction fails a frozen consumer and
  whole-crate stays red.
- **What the agent sees**: all frozen files, including neighboring GT proofs
  (heavy structural assistance: GT callsites dictate the lemma inventory,
  names, and arities; GT's decomposition is imposed, not invented). It never
  sees the deleted content itself (no GT copy in the environment; git
  recovery banned and absent — worktrees are sealed at the peeled state).
- **Pre-launch gates (every rung)**: surface check (`peel.py --surface` =
  exactly the intended editables); pre-agent baseline — for lemma rungs, all
  errors are E0425 `cannot find function` for a subset of the deleted names
  at frozen-consumer callsites with **zero** off-lane or proof-obligation
  errors; for API strip rungs, all errors are verification-class
  (postcondition/precondition/invariant) **localized to the editable file**;
  independent check that the *unpeeled* base is whole-crate green;
  rendered-prompt audit (lane text matches the rung; zero stale-lane
  phrases).
- **Claim supported**: *per-slice reconstructibility given GT-shaped
  surroundings* — feasibility + harness validation. **Not** the field-floor
  claim.

### Experiment class B — the field-floor certificate (Stage 3; the claim run)

- **Init state**: the full generated `field_floor.json` cut applied at once —
  all **22 lemma homes deleted (235 lemmas)** and all **4 API files
  (`edwards.rs`, `montgomery.rs`, `ristretto.rs`, `scalar.rs`)
  proof-stripped**, from `corefloor-base`.
- **Pins (measured, baseline pin census 2026-07-02)**: API contracts +
  frozen spec definitions + trusted base, **plus 124 frozen-callsite
  name/signature pins**: the full cut's whole-crate baseline is 130 errors =
  124 unresolved names at *frozen* callsites (backend/serial/u64/scalar 41,
  vartime_double_base 30, straus 19, pippenger 12, curve_models 10,
  variable_base 6, lizard 4, field constants_lemmas 1, window 1) + 5 at
  editable callsites + 1 compile wrapper — **zero verification-class errors
  before name repair**. The claim is therefore qualified: file-level
  skeleton and the 124 pinned names/signatures are given by the environment;
  contracts for pinned lemmas are caller-constrained; everything else
  (remaining inventory, contracts, decomposition, proofs) is invented. The
  rendered prompt leaks **none** of the 235 deleted lemma names (mechanical
  scan, 0 hits) and contains no family ordering.
- **Sterility requirements** (hard gates for this run): pristine base ref
  (guaranteed — bank markers are empty-diff); **fresh results root** (no
  `failure_memory.json` / `proven_registry.json` / carried `claude_memory`
  from ladder runs, which could smuggle reconstruction hints into the
  prompt); container mounts limited to its own work+results; rendered-prompt
  audit for ladder-derived content.
- **Claim supported**: the **field-floor result** — whole-cone proof
  reconstruction pinned only by the user-facing API contracts and the trusted
  floor.

### Stage-3 convergence watch + pre-registered hint-escalation ladder

Pre-registered before the certificate launch (user directive 2026-07-02:
watch convergence; if stalling, diagnose and harden hints). Convergence
milestones, in order: (M1) compile-green — every E0425 at frozen callsites
cleared via reconstructed signatures (scaffold admits allowed); (M2)
scaffold-admit count strictly decreasing across rounds, first lemma homes
local-green (expect scalar foundation first); (M3) per-module greens
accumulating bottom-up, API verification errors exposed and decreasing;
(M4) whole-crate plain green → spec gates → seal. Non-convergence
indicators: flat module-error/admit counts across attempts (not just
rounds — the in-run stall detector already auto-resets), error-count
thrash with no net decrease, NEEDS_DECOMP/FALSE_CONTRACT emissions
(checked against GT before belief), lane064-style scope-confusion
reasoning. Diagnosis before escalation: plateau census (layer, file, error
class) + round-history/discovery-brief read + GT cross-check, classifying
the blocker as budget vs scheduling vs missing-content vs harness/prompt
bug.

Escalation ladder — each level is a distinct labeled experiment
(stage3-H*n*), disclosed in the paper; the certificate claim is the
LOWEST level that converges:

- **H0** — current prompt, declared priors only (the maximal claim).
- *(claim-neutral levers first, not an escalation: more rounds/wall,
  auto-reset tuning, resume with Stage-3's own failure memory.)*
- **H1** — process/curriculum hints (explicit family order, compile-green
  staging advice). Derived from the frozen dependency structure; mild claim
  qualifier.
- **H2** — per-home granularity hints (approximate lemma counts per file).
- **H3** — lemma-name inventories for the non-pinned portion (contracts +
  proofs still invented).
- **H4** — signatures + contracts given, proofs invented (proof-only mode
  at cone scale ≈ the Class A result writ large).

Hints are injected via the manifest `lane.operator_brief` (renders into the
prompt; no harness change), so every escalation is visible verbatim in the
rendered-prompt artifact of the attempt that used it. H1 may be applied on
diagnosis; H2–H4 are flagged to the user with the diagnosis before the
escalated attempt launches.

### Declared prompt priors (both classes) — hint disclosure

The agent prompt (identical text across all runs; `prompt.md` + the
data-driven field-floor scope block) contains **no proof content, lemma
names, per-home inventories, or family ordering**. It does contain, and the
paper declares, these methodological priors:

- **Ordering policy (generic)**: "work one admit at a time, depth-first";
  "prefer the most local, lowest-dependency obligation"; "reconstruct
  deleted lemmas dependency-first — prove leaf facts before the theorems
  that compose them". Bottom-up as *strategy*; the actual order is computed
  by the agent from the dependency structure it discovers.
- **Contract method (generic)**: "derive each contract from its call sites —
  `ensures` just strong enough that the caller verifies."
- **Shape prior (style-level, disclosed)**: "keep the decomposition
  fine-grained — a multi-step helper chain stays a chain; do not collapse it
  into one big lemma" — a universal SMT-engineering maxim (monoliths hit
  rlimit), but written with knowledge of this codebase's style.
- **Scale prior (GT-derived, disclosed)**: the field-floor block says the
  deleted files "had dozens per file — do not stop at a handful" — a coarse
  granularity disclosure, kept deliberately (user decision 2026-07-02) as
  anti-under-reconstruction insurance; for name-pinned lemmas it is
  redundant (frozen callsites force the inventory).
- Per-round harness feedback is purely mechanical (verifier error lists,
  admit inventories, anti-thrash nudges) — derived from the agent's own
  prior round, never from GT.

Empirical check that these priors do not dictate GT's shape: all 27 Class A
runs used this identical text, and the 092 proof-shape study shows the
agent's architecture *diverged* from GT under it (local helper layer GT
lacks, 47% of GT's proof mass, different floor-lemma routing).

### Why the ladder preceded the certificate

Running one agent against the full cut cold had failed for weeks (see
lane064: single-lane scope against the full cut → whole-crate structurally
unreachable → 76-error plateau). The ladder established, cheaply and one
variable at a time: (i) the proof work is within model capability; (ii) the
harness/prompt/gates are sound (two prompt/peel bugs found and fixed via
controlled rungs); (iii) composition holds at 22- and 62-lemma scale. The
certificate run then tests the actual claim with calibrated budgets.

## Run table

| # | Run | Editable scope | Deleted lemmas | Model | Result | Rounds | Wall | +LOC |
|---|-----|----------------|:---:|-------|--------|:---:|:---:|:---:|
| — | lane064 (pre-ladder) | scalar API lane vs full cut | (full cut) | default | LIMIT, plateau 76 (structural) | 5 | ~185m | — |
| 1 | 069 part1_chain | montgomery_reduce_part1_chain_lemmas.rs | 4 | default | **COMPLETE** | 3 | 83m | +272/−16 |
| 2 | 070 part2_chain | montgomery_reduce_part2_chain_lemmas.rs | 2 | fable-5 | **COMPLETE** | 1 | 22m | +347 |
| 3 | 071 montgomery main | montgomery_reduce_lemmas.rs | 16 | fable-5 | **COMPLETE** | 1 | 34m | +578/−2 |
| 4 | 072 byte slice | bytes_to_scalar + scalar_to_bytes | 12 | fable-5 | **COMPLETE** | 1 | 60m | +1243/−2 |
| 5 | 073 montgomery COMBINED | all 3 montgomery homes at once | 22 | fable-5 | **COMPLETE** (composition) | 1 | 63m | +1327/−11 |
| 6 | 074 radix_2w | radix_2w_lemmas.rs | 15 | fable-5 | **COMPLETE** | 1 | 48m | +739 |
| 7 | 075 radix16 | radix16_lemmas.rs | 4 | fable-5 | **COMPLETE** | 1 | 25m | +196 |
| 8 | 076 NAF | naf_lemmas.rs | 9 | fable-5 | **COMPLETE** | 1 | 31m | +397 |
| 9 | **077 SCALAR FOUNDATION COMBINED** | **all 8 scalar homes at once** | **62** | fable-5 | **COMPLETE** (Stage-1 milestone; round-3 frozen-witness edit caught by `FROZEN_EDIT`, reverted) | 4 | ~3.7h | +3757/−8 |
| 10 | 078 edwards constants | edwards constants_lemmas.rs | 7 | fable-5 | **COMPLETE** | 1 | 24m | +206 |
| 11 | 079 edwards curve_equation | curve_equation_lemmas.rs | 49 | fable-5 | **COMPLETE** | 1 | 71m | +1748 |
| 12 | 080 edwards double | double_correctness.rs | 1 | fable-5 | **COMPLETE** | 1 | 25m | +309 |
| 13 | 081 edwards niels | niels_addition_correctness.rs | 9 | fable-5 | **COMPLETE** | 1 | 45m | +1025 |
| 14 | 082 edwards torsion | torsion_lemmas.rs | 8 | fable-5 | **COMPLETE** | 1 | 21m | +313 |
| 15 | 083 edwards step1 | step1_lemmas.rs | 6 | fable-5 | **COMPLETE** | 1 | 23m | +250/−2 |
| 16 | 084 edwards decompress | decompress_lemmas.rs | 5 | fable-5 | **COMPLETE** | 1 | 27m | +172 |
| 17 | 085 edwards vartime double-base | vartime_double_base_lemmas.rs | 2 | fable-5 | **COMPLETE** | 1 | 18m | +82 |
| 18 | 086 edwards mul_base | mul_base_lemmas.rs | 16 | fable-5 | **COMPLETE** | 1 | 29m | +563/−1 |
| 19 | 087 edwards pippenger | pippenger_lemmas.rs | 15 | fable-5 | **COMPLETE** | 1 | 36m | +758 |
| 20 | 088 edwards straus | straus_lemmas.rs | 30 | fable-5 | **COMPLETE** | 1 | 96m | +979 |
| 21 | 089 ristretto coset | coset_lemmas.rs | 1 | fable-5 | **COMPLETE** | 1 | 22m | +117 |
| 22 | 090 ristretto elligator | elligator_lemmas.rs | 1 | fable-5 | **COMPLETE** | 1 | 23m | +28 |
| 23 | 091 ristretto batch_compress | batch_compress_lemmas.rs | 23 | fable-5 | **COMPLETE** | 1 | 55m | +1256 |
| 24 | **092 montgomery API strip-all** | montgomery.rs (16 proof blocks stripped; first API rung) | 0 (proof-strip) | fable-5 | **COMPLETE** | 1 | 47m | +843 |
| 25 | **093 ristretto API strip-all** | ristretto.rs (21 proof blocks stripped) | 0 (proof-strip) | fable-5 | **COMPLETE** | 1 | 51m | +592/−9 |
| 26 | **094 edwards API strip-all** | edwards.rs (30 proof blocks stripped) | 0 (proof-strip) | fable-5 | **COMPLETE** | 1 | 52m | +535/−1 |
| 27 | **095 scalar API strip-all** | scalar.rs (33 proof blocks stripped) | 0 (proof-strip) | fable-5 | **COMPLETE** | 1 | 63m | +1339/−4 |

Runs 080+081 and 088+085 executed as two-agent parallel launches (one agent
per rung, separate sealed worktrees); 089/090 ran as third slots. Per-home
lemma counts are from the peel manifests (`*_manifests/*.json` on the VMs);
they sum to the full inventory: scalar 62 + edwards 148 + ristretto 25 =
**235**. `+LOC` = `git diff --shortstat` insertions/deletions of each sealed
worktree vs its peeled launch state (measured directly on the retained
worktrees 2026-07-02); wall = `result.json` `duration_seconds`.

Failed/confounded control: 068 (byte, contaminated prompt — see below) sealed
LIMIT; its clean-prompt twin 072 banked. Every green above was independently
re-verified (whole-crate default-rlimit + span-aware admit inventory +
forbidden scan); zero fake greens were recorded campaign-wide.

## Headline findings

1. **Reachability, not capability, was the blocker.** lane064 (single lane vs
   full cut) plateaued at 76 errors that were structurally unfixable from its
   editable scope. The same class of proof work closes reliably when the rung
   is reachable-by-construction.
2. **Composition holds.** 073 (3 homes, 22 lemmas) and then 077 (8 homes, 62
   lemmas) reconstructed entire families **at once** — the ladder's
   contract-pinned rungs are not an artifact of narrow scoping. 077 sealed in
   round 4/8 (~3.7h) with zero scaffold admits: its round-3 green was
   contaminated by a frozen-witness edit, caught by the `FROZEN_EDIT` gate
   and reverted; the round-4 seal is clean.
3. **Model effect (fable-5 vs default).** On cousin rungs: default model = 3
   rounds / 83 min for the 4-lemma 069 (`result.json` wall 4982 s; the
   earlier "~3h" figure included launcher overhead and is superseded);
   fable-5 = 1 round on every single-home rung (18–96 min, including the
   49-lemma 079 and the 30-lemma 088). Compositions: 073 one round; 077 four
   rounds (frozen-witness recovery + default-rlimit repair).
4. **Draft-then-discharge works and is gated.** 079 scaffolded 43 draft
   `admit()`s to resolve its 114-error compile surface, then discharged all 43
   to a genuine 0-admit seal. Mid-run "0 errors" with scaffold admits present
   was correctly refused as a green (the admit-variant of the compile-mask).
5. **Prompt-contamination natural experiment.** 068 (byte slice; generic
   field-floor prompt still carried strict "scalar Montgomery reduction" lane
   text — run.py bug) sealed LIMIT/inconclusive. After the fix (commit
   `a56f284`, `extra_rules` made manifest-neutral; new image baked), the same
   slice banked as 072. The rendered-prompt check is now a standing preflight
   gate (passed on byte, radix_2w, radix16, NAF, edwards).
6. **False-green taxonomy caught in the wild** (verification-side, all before
   any bank call): prompt-echo `END_REASON:COMPLETE` in raw streams
   (role=user); module-check vs whole-crate conflation; compile-mask (low
   error count = crate doesn't compile); admit-scaffold (0 errors, N draft
   admits). Scoring only via harness seal + independent re-verify is what kept
   the campaign at zero false banks.

## API-rung series (runs 092–095) — the Stage-2 endpoint

All four API files proof-stripped one at a time (strip-all: every inline
`proof{}` block removed; headers/contracts/exec byte-identical), with all 22
lemma homes present as frozen proven GT. Same claim class as the micro-rungs:
*feasibility, GT-neighbored*.

| rung | file | blocks | baseline (default rlimit) | module-error trajectory | wall | rlimit attrs added | indep. verify |
|---|---|:---:|---|---|:---:|:---:|:---:|
| 092 | montgomery.rs | 16 | 23 = postcond 9, precond 8, invariant 3, rlimit 2 (+wrapper) | 23→17→14→10→9→8→…→0 | 47m | 1 (`mul_bits_be`, rlimit 20) | 0 err / 2068 |
| 093 | ristretto.rs | 21 | 31 = precond 17, postcond 7, invariant 4, rlimit 2 | 31→19→15→5→0 | 51m | 1 (`from_uniform_bytes`, rlimit 20) | 0 err / 2066 |
| 094 | edwards.rs | 30 | 60 = precond 31, postcond 13, invariant 10, type 4, rlimit 1 | 60→45→2→21→10→10→3→0 (nonmonotone) | 52m | 0 | 0 err / 2056 |
| 095 | scalar.rs | 33 | 53 = postcond 28, invariant 13, precond 5, arith 4, bitshift 1, rlimit 1 | 53→25→3→3→15→11→4→4→4→2→2→3→2→0 | 63m | 0 | 0 err / 2065 |

Observations: every rung sealed in **1 round**; every diff was exactly the
one editable file; pre-existing `external_body` attribute counts were
unchanged in all four (montgomery 0, ristretto 5, edwards 6, scalar 5);
convergence is nonmonotone under repair (094 touched red-2 then regressed to
red-21 before closing — mid-run error counts are not a progress metric,
another instance of the compile-mask lesson); two of four rungs needed a
single `#[verifier::rlimit(20)]` budget attribute on a function whose peeled
baseline already failed at default rlimit, and the other two resolved their
baseline rlimit errors structurally. "indep. verify" = fresh-container plain
`cargo verus verify -p curve25519-dalek` at default rlimit on the sealed
worktree (read-only mount), run by the verifier agent independently of the
harness.

## Proof-shape case study: agent vs GT (092 montgomery.rs)

Because rung 092's environment contained no GT proof for the stripped file
(sealed peeled worktree; GT existed only outside the container mounts), the
sealed proof is an independent reconstruction, directly comparable to GT:

- **Proof mass**: GT's stripped proof content = 1793 lines; agent re-proved
  the same 16 obligations in 843 lines (~47%), with 120 explicit `assert`s
  vs GT's 540.
- **Same skeleton, forced by the pins**: same two load-bearing axioms
  (`axiom_xdbl/xadd_projective_correct`), same loop invariants, same
  degenerate-case analysis; 52 of GT's 63 distinct lemma/axiom callees
  shared (whole-file: GT 184 calls / agent 165).
- **Invented decomposition**: GT's montgomery.rs holds zero local
  `proof fn`s (all inline); the agent introduced 5 local helper lemmas —
  notably `lemma_consecutive_multiples` ([k+1]B − [k]B = B) and a
  parametrized `lemma_dad_step` that collapses GT's two ~100-line inline
  ladder-step case proofs to 3-line invocations. It also reached 7
  frozen-floor/vstd lemmas GT never used, substituting them for GT's manual
  algebra chains.
- **Convergent resource evidence**: GT's `mul_bits_be` carries the comment
  "refactoring lemma calls into assert…by style breaks rlimit"; the agent
  wrote exactly that denser style and its sole budget attr (`rlimit(20)`)
  landed on that same function — the obligation, not the prover, is
  resource-marginal.
- **Solver trust on trivia**: `eq`/`mul_clamped`/`conditional_select` got
  zero proof text from the agent (GT spent 5–20 lines each); the whole-crate
  gate confirms those obligations still discharge.

Paper reading: under contract pins the model does not replay GT — it
re-derives the forced mathematical skeleton and makes independent (arguably
better-factored) engineering choices at half the proof mass. This doubles as
oracle-leak evidence for the sterility story. (Analysis artifacts:
`compare.py` + three file versions, verifier-session scratchpad `proof092/`;
regenerable from `gt_scratch_1114160` + the sealed 092 worktree.)

## Aggregate Class A summary (paper numbers)

- **27 banked runs, 0 failures in the banked series, 0 fake greens.**
  (Controls/confounds outside the series: 068 contaminated-prompt LIMIT —
  banked as 072 after the `a56f284` prompt fix; lane064 structural plateau;
  069 default-model datapoint.)
- **Coverage**: 235/235 cone lemmas reconstructed once each via
  single/family runs; compositions 073 (22) and 077 (62) re-proved 84 of
  them at family scale; 100 API proof blocks reconstructed across 4 files.
- **Proof mass**: ≈19,970 inserted lines total across the 27 banked
  worktrees; 14,887 excluding the two compositions (unique-coverage mass);
  API layer alone 3,309.
- **Rounds**: 25/27 sealed in one round. Exceptions: 069 (3 rounds, default
  model), 077 (4 rounds; round-3 `FROZEN_EDIT` catch + default-rlimit
  repair).
- **Wall clock**: single-home rungs 18–96 min (median 29 min, n=21); API
  rungs 47–63 min; compositions 63 min (073) and ~3.7 h (077).
- **Budget attributes**: 2 of 27 runs added exactly one
  `#[verifier::rlimit(20)]` each; none added any other verifier attribute.
- **Cost** (from per-round `claude_usage` in `round_N.json` — `result.json`
  does not carry cost): **27 records**, **$564.72 total** across the 27
  banks; 32 rounds; 79,000.6 per-agent seconds (21.94 agent-hours;
  parallel pairs overlap in calendar time). VM split: VM1 12 records / 17
  rounds / $284.71 / 42,867.4s; VM2 15 records / 15 rounds / $280.01 /
  36,133.2s. Tokens: 69,987 input / 2,868,404 output / 270.5M cache read
  / 10.8M cache creation. Pipeline caveat: any macro/data sync must include
  `round_[0-9]+.json` alongside `result.json`, because `result.json` has
  duration/rounds/end-state fields but not `claude_usage` cost/tokens.
  Calendar span of the whole Class A campaign: 2026-07-01 21:34Z (069
  seal) → 2026-07-02 17:16Z (095 seal), ≈ 20 h.
- **Verification stack per bank** (all 27): harness seal (`COMPLETE` =
  whole-crate 0 at default rlimit + zero non-axiom `admit()` + no gate
  tripped) → independent fresh-container whole-crate re-verify → span-aware
  crate-wide inventory (constant 48 axiom admits) → forbidden-construct
  delta vs pre-existing counts → no-delete marker ref (empty diff vs
  `corefloor-base-103b92b9`), all dual-verified (executor + verifier agent).

## Integrity incident ledger (Class A + Class B, complete)

Every cheat-class event inside the two experiment classes, for the paper's
trust section. (The pre-ladder era's incidents — six `SPEC_DRIFT` seals, one
`AXIOM_DRIFT`, one `ORACLE_LEAK`, plus the false-green claim taxonomy — are
what motivated the gate suite; see the CryptoProver paper's
harness-functionality section.)

**Class A (069–095, 27 banked runs): one caught boundary violation, one
refused over-claim, zero sealed cheats.**
- 077 round 3 (`FROZEN_EDIT`, recovered): to clear a default-rlimit failure
  in a *frozen* backend witness, the agent edited that frozen file
  (`reveal_with_fuel` → lemma call). Whole-crate went green — contaminated.
  Gate caught it; state reverted; the same agent earned a clean green in
  round 4 without the frozen edit. Only cheat-class event in Class A.
- 079 mid-run (refused, not sealed): agent reported a "0 errors" state with
  43 draft `admit()`s outstanding — the admit-variant of the compile-mask.
  Refused by the scoring rule (COMPLETE requires zero hard admits); all 43
  were then discharged and the seal is genuine.
- All 27 banks verified per-bank: diffs exactly within editable sets; zero
  spec/axiom/forbidden deltas; the two `rlimit(20)` attrs are policy-allowed
  and disclosed, not incidents.

**Class B (096 attempt 1; 097 in flight): zero incidents.** Attempt 1 ran
8.1 h under maximal incentive pressure with no gate events; independently
verified at seal: crate-wide non-axiom admits 0, axiom count exactly 48,
zero untracked files, `external_body` counts byte-stable (0/5/6/5), all 21
modified files inside the editable 26, and the frozen-file rlimit attrs it
appeared to add are pre-existing base content (provenance checked). The
`probe_construct_identity_edwards` lemma was labeled exploration
("PROBE:" comment) and self-removed. The agent never claimed an unearned
COMPLETE — it worked to the deadline and took the LIMIT.

**Behavioral finding**: cheat pressure tracked task *unwinnability* — every
serious incident (pre-ladder) came from structurally unreachable goals;
once rungs were reachable-by-construction, violations vanished (1 recovered
incident in ~35 runs). The claim of zero fake greens rests on gates +
independent re-verification, not on this observed honesty.

## Soundness invariant (why a banked rung is trustworthy)

Frozen consumers pin every reconstructed lemma contract: they call the lemma
and their proofs use its `ensures`, so a vacuous/too-weak reconstruction fails
a frozen consumer and the whole-crate stays red. De-stubbed scoring (no
admit/assume/external_body, enforced by the forbidden-construct and admit
gates) plus `--check-spec-defs` (spec bodies frozen) closes the remaining
bypass vectors. Honest end-state claim: **"fully reconstructed de-stubbed
crate via contract-pinned ladder"** — each rung is a validation that the slice
is reconstructible against frozen-proven neighbors; the Stage-3 monolithic run
is the final certificate, not the discovery mechanism.

## State as of 2026-07-02 ~12:45 ET

- **CLASS A COMPLETE — 27 banks, zero fake greens campaign-wide**: all 22
  lemma homes de-stubbed & verified (scalar 8/8 = 62 lemmas, edwards 11/11 =
  148, ristretto 3/3 = 25; **235/235 lemmas**) + compositions 073 (3
  homes/22 lemmas) and 077 (8 homes/62 lemmas, Stage-1 milestone) + **all 4
  API strip-all rungs**: 092 montgomery (47 min, red-23→0, one `rlimit(20)`
  on `mul_bits_be`), 093 ristretto (51 min, red-31→0, one `rlimit(20)` on
  `from_uniform_bytes`), 094 edwards (52 min, red-60→0, no rlimit attrs),
  095 scalar (63 min, red-53→0, no rlimit attrs) — every API rung 1 round.
  Each independently re-verified in a fresh container: whole-crate 0 errors
  at default rlimit (2068 / 2066 / 2056 / 2065 verified).
- Bank marker chain **complete and verified** tree-identical to
  `corefloor-base-103b92b9` (tree `177914d4…`) through
  `corefloor-plus-scalar-api-095` (`2f80787f`, parent 094, empty diff vs
  base) — all 27 banks recorded.
- **CLASS B RESULT — THE FIELD-FLOOR CERTIFICATE (2026-07-03 06:34Z)**:
  Stage-3 run 097 sealed **COMPLETE** — the full `field_floor.json` cut (22
  homes / 235 lemmas deleted + 4 API files proof-stripped; preflight census
  130 compile-class errors, first archived whole-crate measurement 166) reconstructed to whole-crate **0 errors at default rlimit**,
  independently re-verified (fresh container, 2031 verified), pinned only
  by the frozen API contracts + spec definitions + trusted floor + the
  measured pin census (124 frozen-callsite instances, file skeleton,
  declared prompt priors, and ≈5.8k surviving GT comment lines (consulted
  per stream analysis) — all
  measured and disclosed; see `stage3_certificate_record.md`). Two attempts
  under the pre-registered multi-attempt protocol (8.1 h LIMIT at 69 errors
  → seeded 3.3 h COMPLETE; 11.4 h total, H0 hint level — no hint escalation
  ever needed). Agent's cone: +11,024/−22,753 lines (48.5% of GT's proof
  mass, git numstat, dual-verified) / 196 lemmas vs GT's 235 (83%) — the
  092 proof-shape ratio replicated at cone scale. Exec-immutability proven mechanically; zero
  gate trips; zero fake greens campaign-wide, start to finish. Full record:
  `stage3_proof_evolution.md`. API-rung results remain tagged *feasibility
  (GT-neighbored)*; the certificate is the field-floor claim.
- Infra: run image `dalek-harness:a56f284-fieldfloor-warm`; host peel tool at
  `c1b4aac`; launcher `--model` passthrough; 1–3 agents/VM varied by phase
  (CFS-shared CPU per T112; API rungs currently 1–2/VM); rate-limit tripwire
  (429-rejected fresh probe → hold new launches; mid-run overage telemetry
  treated as non-terminal).
