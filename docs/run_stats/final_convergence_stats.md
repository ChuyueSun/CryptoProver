# Field-floor convergence experiment — FINAL stats (closeout, 2026-06-29)

> **Historical context.** This is the closeout of the *June 2026* field-floor
> campaign era, which ended in non-convergence and motivated the ladder
> methodology. The field-floor cut was subsequently **solved** in the July
> Stage-3 certificate runs (096/097, whole-crate 0 errors, sealed COMPLETE) —
> see `stage3_certificate_record.md` and `ladder_stage1_results.md`. This
> record is retained as the failure baseline and gate-validation evidence.

**Outcome: NON-CONVERGENCE (accepted by user as the experiment's result).**
On the field-floor cut — reconstruct the *entire* above-field proof cone over
`ristretto.rs` (~26 editable lemma/spec files) from a deleted/admitted starting
state, scored by **whole-crate verus errors → 0 AND non-axiom hard `admit()` → 0**
— the agent does **not** converge. (The cut is `peel_manifests/field_floor.json`
**depth 2 = proofs + lemmas**: the real task is **reconstruct 168 deleted lemmas,
contracts included, AND discharge ~66 admit-stubs** across 235 in-scope lemmas —
the `admit()` count below tracks only the stub portion; see the §3 correction.) Across 9 sealed docker runs + 1 bare-metal
control, **0 reached a complete proof**. The integrity gates caught every cheat;
no false green was ever sealed. The wall is **model capability on multi-step proof
decomposition**, not tooling.

> **Reading the numbers.** A low whole-crate *error* count is NOT "near complete":
> `admit()` trivially satisfies any obligation, so a run can show few errors while
> leaving the real proofs unwritten. Always read **errors AND hard-admits-remaining
> together**, and use the **settled** whole-crate `-p` value (not module/rlimit
> checks, not the series minimum — mid-edit verifies that fail to *compile* report
> spuriously low counts).

## 1. Completion metric

Definition: `success == true && final_hard_admits_remaining == 0 &&
final_error_count == 0 && no integrity drift`.

**Result: 0 / 7 scored field-floor runs complete (0 / 8 incl. the gcp14
control); 3 further runs voided (§2).** No run ever drove hard admits below the
**66-admit seed baseline**; the plateau band is **66–73 hard admits**, with one
admit-propagation blowup to 187. Net convergence vs the seed ≈ **0**.

## 2. Lineage table

Seed baseline = 66 hard admits. All field-floor runs target `ristretto.rs` cone.

### Scored runs

| Run | end_reason | rounds | hard admits (final) | whole-crate err (settled) | integrity | image | tapped |
|-----|-----------|:-:|:-:|:-:|--|--|:-:|
| **gcp14** (bare-metal control) | LIMIT | 4 | **0** | 214 strict / 203 rlimit-gate | clean | n/a | no |
| ff_docker_resume_001 | SIBLING_VERUS_FAIL | 1 | 66 | 2 (probe) / canon 4 | clean | f3bfa28 | yes |
| ff_docker_resume_002 | LIMIT | — | 73 | — | clean | f3bfa28 | yes |
| ff_docker_resume_003 | LIMIT | — | **187** (admit-prop blowup) | — | clean | f3bfa28 | yes |
| ff_docker_resume_004 | LIMIT | — | 66 | — | clean | f3bfa28 | yes |
| ff_docker_resume_006 | LIMIT | — | 71 | — | clean | f3bfa28 | yes |
| ff_docker_resume_007 | LIMIT | — | 70 | — | clean | f3bfa28 | yes |
| ff_docker_resume_009 | LIMIT | 1 | **69** | **23** (last-recorded; see §3 caveat) | clean (spec_drift=0) | f3bfa28 | yes |

### Voided runs (integrity gate / infra anomaly — score discarded, NOT a convergence data point)

| Run | end_reason | note |
|-----|-----------|------|
| ff_docker_resume_005 | **AXIOM_DRIFT** (68) | new `axiom_*` introduced → non-promotable, voided |
| ff_docker_resume_008 | **SPEC_DRIFT** (69) | spec header/body drift → non-promotable, voided |
| ff_docker_resume_011 | **PROCESS_CROSSTALK** (69; err 11; spec_drift 11) | `f3bfa28-fix`, rc=-9 at the 180-min wall. *Twice tainted:* (a) PROCESS_CROSSTALK is a **detector false-positive** — a benign `verus_check.py --help` probe that `verifier_policy_hook.py` allows but `run.py`'s `detect_process_crosstalk` did not exempt (codex patched locally: run.py + `test_admits.py:2504`; **NOT yet in the image**); (b) 11 real blocking SPEC_DRIFTs, all `pub proof fn`→`pub(crate) proof fn` visibility churn (constants/niels/montgomery). Underlying admit count was the same **69 plateau**. |

`ff_docker_resume_010` was a stray launch, killed (not a data point). The voided
runs are the *evidence the gates work*: the cheapest paths to a green (weaken a
spec / invent an axiom / let a benign probe slip) were caught **every time** — no
voided run was ever promoted to COMPLETE. (run011's crosstalk was a *false
positive*, not a cheat — so its underlying state still corroborates the 69
plateau; it just can't be cited as a clean scored run.)

**Scored field-floor runs = 7** (001/002/003/004/006/007/009; + gcp14 control);
**voided = 3** (005/008/011). Completion remains **0/7 scored (0/8 incl. control).**

## 3. Spec-vs-proof split (the headline: "how much good spec vs how much proof")

Counted from the **true start = the deleted cone**, not the 66-admit seed.
Canonical run009 (`final_*` fields in `result.json`): `final_hard_admits = 69`,
`final_intentional_axiom = 5`, `final_error_count = 23`, `final_spec_drift = 0`,
`final_verus_okay = False`.

> **⚠ CORRECTION (2026-06-29, baseline-delta + manifest audit).** Two errors in
> an earlier version, both found by user challenge:
>
> **(a) The task is bigger than "66 admits."** Field-floor is `peel_manifests/field_floor.json`
> **depth 2 = proofs *and* lemmas** over **235 in-scope lemmas**. Measured against
> the cold seed: **56 present+admit-stubbed (~66 canonical), 11 present+proven
> (seed-provided), and 168 DELETED outright** (signature + `requires`/`ensures` +
> body all stripped). So the agent's real task is **reconstruct 168 deleted
> lemmas — contracts included — AND discharge ~66 admit-stubs**, not "fill 66
> bodies under frozen contracts." (The seed even has `E0425 cannot find function`
> from references to deleted lemmas.) "Frozen contracts" was wrong; only the 56
> stubbed lemmas have frozen contracts.
>
> **(b) The "~10 proven bodies" I credited to the agent are seed-provided**
> (`seed_a_run003` retained scaffolding), not authored by any run. A per-family
> `diff` of every sealed run vs the cold seed shows the *only* file any run ever
> changed is `pippenger_lemmas.rs`.
>
> **Agent's actual output, correct lens:** of 168 deleted lemmas it reconstructed
> **~5** (all pippenger) and **proved 1**; of ~66 admit-stubs it discharged **0**;
> it engaged ~5 of 235 in-scope items. The table below is the **sealed-state
> inventory** (≈ seed state), **not** agent reconstruction; see baseline-delta below.

**Per-family sealed-state inventory (run009) vs ground truth (ChuyueSun/dalek-lite
`corefloor-base-103b92b9`)** — LOC/fns/admit-free counts describe the *seed-derived
sealed state*, most of which the agent never touched:

| Family | LOC | proof_fns | admit-free bodies (seed-provided) | GT LOC | GT proof_fns | GT admits |
|--------|:-:|:-:|:-:|:-:|:-:|:-:|
| curve_equation | 427 | 29 | 0 / 29 | 3296 | 53 | 4 (axiomatized) |
| montgomery_reduce | 350 | 14 | 1 / 14 | 1202 | 16 | 0 |
| straus | 572 | 13 | 6 / 13 | 1322 | 30 | 0 |
| niels_addition | 194 | 5 | 0 / 5 | 1526 | 9 | 0 |
| vartime_double_base | 216 | 9 | 2 / 9 | 112 | 2 | 0 |
| pippenger | 498 | 5 | 1 / 5 | 1316 | 15 | 1 |
| **Σ (6 families)** | **2257** | **75** | **~10 / 75 (all seed-provided)** | **8774** | **125** | **6** |

**Baseline-delta: what the agent actually did (admit() per family, cold seed vs
sealed runs).** This is the real progress measure:

| family | seed | r004 | r007 | r009 | r011 | agent edits? |
|---|:-:|:-:|:-:|:-:|:-:|--|
| curve_equation | 29 | 29 | 29 | 29 | 29 | **0 diff lines — never touched** |
| montgomery_reduce | 13 | 13 | 13 | 13 | 13 | 11 lines edited, **0 discharged** |
| straus | 7 | 7 | 7 | 7 | 7 | **0 diff lines — never touched** |
| niels / vartime / torsion / bytes_to_scalar | 5/7/3/3 | = | = | = | = | unchanged |
| **pippenger** | **1** | 1 | 5 | 4 | 4 | **+131 lines — the only file ever changed** |

Hard-admit concentration (run009, 69 total): curve_equation 25 + montgomery 13 =
38/69 (55%); +straus 7 +vartime 7 = 52/69 (75%) — **and none of those 52 were ever
attempted.**

**What this says (corrected):**

- **The agent discharged ~0 of the 66 seeded obligations.** Across all sealed
  runs the only file changed is `pippenger_lemmas.rs`: the deleted Pippenger API
  lemmas were *recreated* as admitted stubs (seed 1 admit → 5, run007), then
  exactly **one** was proven (5 → 4, run009/run011). That recreation is *why the
  net rose to 69* (added admitted scaffolding > discharged). Net proof output of
  the entire campaign ≈ **one** pippenger sub-lemma. The two largest families
  (curve_equation's 29-lemma projective↔affine chain, straus's 7) were
  **byte-identical to the seed — never even edited.**
- **The lemma structure was seed-provided, not agent-reconstructed.** The signatures
  + frozen contracts that make the crate compile come from `seed_a_run003`; the
  agent did not rebuild them (it never touched those files). The only structure the
  agent *did* build is the recreated Pippenger API (which then collapsed GT's
  15-fn bridge to 5 and could not be proven). So "structural reconstruction" is a
  property of the seed, not an agent achievement — except in pippenger, where it is
  divergent and unproven.
- **Proof / bodies = ~0 net discharged by the agent.** The ~10 admit-free bodies
  in the sealed state (6 straus, 1 montgomery, 2 vartime, …) are **seed-provided**,
  not agent-authored (the agent never edited those files — see the baseline-delta
  table). The agent's own net proof output ≈ **one** recreated pippenger
  sub-lemma; curve_equation's 29 lemmas remain admit-stubs and were never touched.
- **"How much further":** GT proves these 6 families in **~8774 LOC / 125
  proof_fns**; the sealed state holds **2257 LOC** (mostly seed-provided signatures
  + `admit()` stubs). The genuine-proof gap the *agent* would still need to close
  is essentially the **full ~8774 LOC** (it discharged ~0), concentrated in the
  projective↔affine curve-equation chain and the montgomery bit/nonlinear
  reductions — neither of which any run attempted.

> **§3 caveat (codex-corroborated; needs settled re-verify to close).**
> `result.json` records `final_error_count = 23`, but the round was SIGKILLed at
> the wall (`returncode = -9`, `duration = 10800.25 s`) mid-verify, so 23 is the
> **last-recorded/sampled** whole-crate count, not a settled value. **Codex
> independently confirmed** (AGENT_DEBATE 12:02 VM1_STATS_AUDIT) the canonical
> `result.json` fields (LIMIT / hard=69 / axiom=5 / err=23 / spec_drift=0) and
> the `round_1.json` `rc=-9` + only 24 stored `verus_errors`, and **concurs 23 is
> a killed-round sample, not the proof frontier.** An independent re-measurement
> on the sealed tree reported **~173–192 verus errors *with the 69 admits still
> in place***, and codex found corroborating evidence in `cli.log:89-91` — the
> final gate interleaved a broad `--rlimit 80.0` check that returned
> **`okay=False errors=175`** — i.e. the reconstructed contracts are flawed
> (dropped preconditions / weakened ensures), so ~180 obligations fail *beyond*
> the admitted bodies. The 175/≈180 figure is **corroborated but not settled**
> (the cli.log command/result pairing is concurrent/interleaved and the round was
> SIGKILLed), so treat "≈180 errors-with-admits" as **provisional** pending a
> noncompeting settled `-p` re-verify (deferred, see §6).

## 4. Capability frontier (GT-backed, qualitative)

**CAN do:** simple induction / `decreases` recursion; introduce an auxiliary
recurrence (run011 even *introduced* the GT aux helper `pippenger_weighted_from`);
single-step algebraic rewrites; the easier straus column-sum bodies.

**CANNOT do:** chain a **multi-lemma helper ladder to completion**. GT proves
`lemma_pippenger_horner_correct` via a bridge through `pippenger_weighted_from`
(GT pippenger = 1 admit); the agent introduced the same aux recurrence but left
**3 of its obligations admitted** (run011 `pippenger_lemmas.rs:379/406/488`;
run009 `:435/:460/:495`). Same pattern in the curve-equation
projective↔affine chain (0/29) and the montgomery multi-step reduction (1/14):
the agent can state the decomposition but cannot discharge the multi-hop proof.

## 5. Paper blockers (evidence-backed)

1. **Capability ceiling — near-total failure to even attempt the obligations.**
   The strongest blocker: across all sealed runs the agent **discharged 0 of 66
   seeded admits** and only ever edited **one file** (`pippenger_lemmas.rs`). The
   two largest families (curve_equation 29, straus 7) were byte-identical to the
   seed — never touched. *Evidence:* §3 baseline-delta table.
2. **Multi-step decomposition is where the one attempt died** — in pippenger, the
   agent recreated the deleted API (collapsing GT's 15-fn bridge to 5) and proved
   only 1 of 5; it introduced the aux recurrence `pippenger_weighted_from` but left
   3 obligations admitted (§4). It cannot chain a multi-lemma ladder to completion.
3. **Admit concentration** in the projective↔affine curve chain +
   montgomery bit/nonlinear: 38/69 (55%) in 2 families, 52/69 (75%) in 4 — **none
   of which any run attempted.**
4. **Contract quality is a SEED property, not an agent achievement.** The compiling
   signatures+contracts are seed-provided (the agent didn't rebuild them); the
   ≈180-errors-with-admits signal (provisional, §3 caveat) therefore reflects the
   *seed's* contracts, not agent reconstruction. The only agent-built structure is
   the divergent, unproven pippenger API (15→5).
5. **Agent admit-props, does not axiom-invent** — when stuck it stubs with
   `admit()` (and propagates: run003 → 187) rather than forging axioms; the
   integrity gates make axiom/spec cheating non-viable (runs 005/008 voided, never
   sealed green). Honesty of the *negative* result is gate-guaranteed.
6. **Fixed image (f3bfa28-fix) removed friction but not the plateau.** The fix
   bundle (A2 verus_check target/project resolution → 0 FILENOTFOUND in run011;
   A1-BASH_ENV `/work` cwd; 5-rule generic proof-craft block, leak-free; panic-kind
   diagnostics) eliminated the tooling false-failures but run011 still plateaus at
   69 — **capability, not tooling.**

## 6. Deferred / open (do not block the paper)

- **VC-level AIR partial-progress metric — first live sample landed; sealed
  full-cone figure still to do.** Per codex (AGENT_DEBATE 12:02) the user wants
  partial progress scored on **real AIR VCs**, not the Verus `verified` line and
  not just admit/spec counts. **Corrected method (codex 12:51 VM1_VC_METRIC_CORRECTION):**
  `progress = (V_now − failing_AIR_obligations) / V_gt`, where `V_now =` count of
  `(location …)` nodes in the per-module `*-final.air` logs (`verus … --log
  air-final`), and the numerator subtracts the **Verus/AIR `errors` count** (the
  `verification results:: N verified, M errors` line) — **NOT** the harness
  `verus_check.py` diagnostic-line `error_count` (e.g. `errors=31`), which stays
  the operational triage frontier only. Restrict headline counts to
  baseline-snapshot fn names; report helper-lemma VCs separately.
  - **Latest live sample (codex 13:10/13:24, run011 Ristretto scope —
    provisional, NOT the closeout headline):** isolated post-fix spectator
    snapshot `/tmp/codex_vm1_postfix_1782752483` produced
    `ristretto-final.air` = 386 locations +
    `ristretto__decompress-final.air` = 58 → `V_now = 444`; Verus reported
    `50 verified, 14 errors`; GT Ristretto-prefix denominator `V_gt = 793` ⇒
    **(444 − 14)/793 ≈ 54.2%**. This is a *ristretto-module* snapshot against
    the *GT ristretto prefix*, not the whole field-floor cone — a
    method-validation data point, not the sealed result.
  - **Full-cone GT denominator measured (codex 14:34 VM1_NOTE):** parsing the
    saved GT AIR against the 27-file `spec_snapshot.json` (483 sigs / 398 base
    names), the **baseline snapshot-function-restricted `V_gt ≈ 4,870`**
    (module-root-only 4,771; the all-crate aggregate is 20,476 and the
    file-level snapshot aggregate 7,332/7,483 — neither is the headline). Top GT
    obligations by root AIR: montgomery `differential_add_and_double`=219,
    `mul_bits_be`=193, scalar `non_adjacent_form`=189, `as_radix_2w`=182,
    `elligator_encode`=167, edwards `mul_base`=142, `montgomery_invert`=130.
  - **Still to do (the closeout headline = `V_now` over the full cone):** the same
    AIR count over **all baseline-snapshot fn names across the ~26 editable files**,
    measured on the **sealed run009 tree** (the only *compilable* sealed state —
    run011/run012 are spec-drift-/assume-tainted and cannot produce valid AIR), in
    a throwaway `dalek-harness:f3bfa28-fix` container, then `progress =
    (V_now − M_errors)/4,870`. run011 has sealed, but **a wrong-lane run012 is now
    live on VM1** (and codex's VM2), so still hold the measurement off VM1 to avoid
    competing. Until `V_now` lands the full-cone raw-VC progress is `NOT COUNTED
    YET` — not zero, not paper-ready.
- **Settled whole-crate `-p` re-verify** of the sealed run009 tree to resolve the
  §3 caveat (23 vs ~180 errors-with-admits).
- **run011 seal** — fold its final `result.json` in (currently provisional 69).

---
_All §3 per-family agent numbers measured on the sealed `ff_docker_resume_009`
work tree; GT numbers on the local `scratchpad/gt` cache of
`ChuyueSun/dalek-lite@corefloor-base-103b92b9`. Lineage hard-admit values from
prior session archive + this session's seals. **Pending codex verification of
every number + blocker before paper-ready (AGENT_DEBATE).**_
