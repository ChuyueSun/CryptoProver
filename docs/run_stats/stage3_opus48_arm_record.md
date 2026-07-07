# Stage-3 field-floor — opus-4.8 arm record (098–106, A1–A9)

The canonical record of the **Class B / opus-4.8** arm: the same field-floor
cut as the fable-5 certificate run, driven to convergence over nine
seed-chained attempts under the user's resume-forever standing order.
Companion docs (fable-5 arm, same cut): `stage3_certificate_record.md`
(packet/battery template), `stage3_proof_evolution.md` (fable proof
evolution + obligation-hardness ranking), `gt_divergence_097_certificate.md`
(architecture vs GT). Primary evidence: the per-round rows, seal batteries,
and incident notes in `stage3_local_evidence/ablation_098_tracking.md` and
the append-only `AGENT_DEBATE.md` ledger (bilateral, claude-relaunch +
codex). **Evidence archive (durable)**: VM1
`stage3_evidence_backups/opus48_098_106_endpoint/` — all 9 per-attempt
FINAL patches (A1 `e7ee333f` … A9 `67e949af`), the A9 seed-full patch
(`2b4fa141`), `a9_certverify_whole.json`, per-attempt
result/round/cli.log bundles, ledger + tracking-file snapshots,
`SHA256SUMS`, and a `MANIFEST.md` mapping each artifact to the record
section it supports; mirrored off-VM and distributable as a tarball on
request (raw `claude_raw` round streams stay in the VM run dirs, named in
the manifest). Status: numbers are sourced to primary artifacts (result.json /
round_N.json / cli.logs / patch files) or to attributed ledger/tracking
rows; the high-risk and flagged fields (durations, patch shas, censuses,
frontier counters, cross-arm comparisons) were independently re-checked
by codex (ledger entries [codex 14:57], [codex 15:02] 2026-07-06), and
items that resisted independent derivation are stated with their method
and caveat inline rather than as bare numbers.

## Result (headline)

**A9 (`field_floor_stage3_106_480_opus48_a9`) sealed `COMPLETE` 2026-07-06
~08:24Z**: whole-crate 0 errors at default gate, `verified_count=2114`,
`truncated=False`, `final_hard_admits_remaining=0`,
`final_intentional_axiom_admits=5`, `final_spec_drift_count=0`, 19 rounds,
29,449 s (~8.2 h). Independent fresh-container re-verify (image
`ebff7cd-fieldfloor-warm2`, `:ro` mount, copy-inside, whole-crate
`--timeout 2400`): `okay=True, error_count=0, truncated=False`
(`certout_a9/a9_certverify_whole.json`). Arm totals: 9 attempts,
2026-07-03 → 2026-07-06, all H0/unassisted.

## The cut (identical to the fable-5 certificate cut)

Full generated `field_floor.json` manifest at `corefloor-base-103b92b9`:
22 lemma homes deleted (235 GT lemmas: signature+contract+body), 4 API
files proof-stripped, editable set = exactly those 26 files, everything
else frozen (`FROZEN_EDIT` guard), frozen `spec fn` definitions
(`--check-spec-defs`), trusted floor = vstd + field layer + exec code + 48
`axiom_*`. Pins, priors, and the GT-NL-comment disclosure are as measured
in `stage3_certificate_record.md` §"Measured pins" — same init state, so
they carry over verbatim.

## Arm design (how this differs from the fable arm)

- **Model**: `claude-opus-4-8` (fable arm: `claude-fable-5`). Same harness,
  same prompt, same gate config, nominal 24-round / 480-min per-attempt
  caps after A1 (A1 ran an earlier 8-round-cap config; `result.json`
  `rounds_used=8`, wall unused), with reset-cap and wall early stops as
  recorded per attempt in the table below.
- **Resume-forever seed chain** (user standing order, 2026-07-03 22:12 ET):
  on every LIMIT seal, next attempt = fresh peel + cumulative
  `--seed-wip` patch (prior seedbuild + prior FINAL patch) + prior
  `failure_memory.json`; pre-agent in-container seed-continuity gate
  (whole-crate must equal the sealed frontier) before each launch.
- **Isolation**: docker per-agent (1 container/target, sealed worktree,
  `--require-tap`), VM1 `andtruth-benchmarks-x86` only. Sterile: no
  readable proven tree; GT-comment probes in every battery.
- **Image lineage**: `a56f284-fieldfloor-warm` (A1–A4) →
  `0b3aaf1-fieldfloor-warm2` (A5–A7; + c1b4aac strip_specs parity + the
  truncation fix 2aa7cda + num-bigint 0.4.6 `cargo fetch --locked`,
  `CARGO_NET_OFFLINE=true`) → `ebff7cd-fieldfloor-warm2` (A8–A9; + the
  verifier stdout-redirect policy hook). Both mid-arm harness fixes were
  codex line-reviewed, dual-signed, and covered by the user's verbatim
  grant ("you have my permission to fix harness and relaunch till
  converge"); no other harness delta shipped mid-arm.

## Attempt table

| A | run-id | sealed (Z) | rounds | dur | end | frontier at seal (counter) | FINAL patch sha (prefix) | headline |
|---|--------|-----------|--------|-----|-----|---------------------------|--------------------------|----------|
| 1 | …098 | 07-03 15:03 | 8 (cap) | 3.967h | LIMIT | 177 raw / 166 v-class; 7 hard admits left | e7ee333f | first descent 202→177; stubs 37→36; recorded cost $69.65 |
| 2 | …099 | 07-04 02:04 | 16 | 8.069h | LIMIT | 58 raw; admits 0 | 20cf2295 | 177→58; edwards 30→24 at wall; resume-forever ordered |
| 3 | …100 | 07-04 10:36 | 24 (cap) | 8.059h | LIMIT | 20 raw / 17 v-class | 0fc28023 | probe→plateau→stub sweep; edwards green r16; recorded cost $137.85 |
| 4 | …101 | 07-04 16:36 | 12 (reset-cap) | 5.76h | LIMIT | "2" **TRUNCATED lower bound** (pre-fix) | 3bed30d6 | pippenger cluster + montgomery arc + scalar-byte lanes; hollow 12→1 |
| 5 | …102 | 07-04 22:36 | 24 (cap) | 3.86h | LIMIT | 2 (truncated) | 939b5ead | **T126 confabulation headline**: r8–r24 instant-LIMIT loop on a de-novo false green memory; ristretto 16→8 real work r2–r7 |
| 6 | …103 | 07-05 06:54 | 16 (wall) | 8.090h | LIMIT | 31 raw / 17 / 26 v-class, **first honest count** (truncation fix live: 54 @ r5 → 31), verified 1978 | 2a5f9301 | scalar 41→~12; montgomery hash+mul_base; 4 homes green |
| 7 | …104 | 07-05 15:24 | 13 (wall) | 8.263h | LIMIT | r12 last honest scored: 16 raw / verified 2003 (result.json final raw=1 is a truncated 900s-timeout lower bound, held indeterminate) | 3de28777 | montgomery MODULE GREEN; ristretto →3; +4 rlimit attrs |
| 8 | …105 | 07-05 23:54 | 13 | 8.03h | LIMIT | 3 raw / 2 / 0 v-class, untruncated, verified 2082 | 3b4f84e4 | scalar MODULE GREEN; coset hollow stub cleaned; zero scratch (hook fix validated) |
| 9 | …106 | 07-06 08:24 | 19 | 8.18h | **COMPLETE** | **0**, verified 2114 | 67e949af | L1731 loop falls; see endgame below |

Frontier series (whole-crate raw counter, with the A4/A5 truncation
relabel): 202 → 177 → 58 → 20 → (2, truncated lower bound) → (2, truncated)
→ 31 honest → 16 → 3 → 2 → **0**. The A4/A5 "2"s were pre-truncation-fix
illusions; A6's first honest in-run measurement was 54. Fable-5 analog on
the same cut: 166 → 69 → 0 in two attempts.

## A9 endgame (rounds 9–19) — how the last wall fell

Sole obligation r9–r18: `ristretto.rs:1731` batch-compress `while` loop,
"Resource limit (rlimit) exceeded", plus its compile wrapper
(`diagnostic_kind_counts = {resource-limit: 1, build-wrapper: 1}` every
round). `verified_count` series r9→r19: 2083, 2085, 2086, 2086, 2092,
2096, 2100, 2104, 2107, 2112, 2114 — bank growth against a fixed wall,
`verification_error_count=0` throughout (zero logical failures; pure
SMT-resource residual).

- **r13–r15 (leaf banking)**: agent authors a decomposition roadmap;
  banks `lemma_batch_diff_of_squares`, `lemma_a_minus_d_square`,
  `lemma_batch_u1_is_square`, `lemma_invsqrt_of_nonzero_square` (+4 more:
  `lemma_batch_f_h_nonzero`, `lemma_batch_body_zero`,
  `lemma_ristretto_compress_extended_value`, `lemma_compress_affine_zero`)
  — both sides of the `eg/u2==0` edge case + denominator bridge.
- **r16 (rotate=false)**: monolithic `lemma_generic_rotate_false` rlimits
  at default/80/300 → decomposed (`lemma_z_inv_square_form`,
  `lemma_rf_s_match`) → real assert → green.
- **r17 (rotate=true crux)**: `lemma_rt_n_times_a`, `lemma_rt_crux`,
  `lemma_rt_s_match` (10-probe grind on the τ=false reduction block).
- **r18 (assembly)**: `lemma_generic_rotate_true` rlimits at 200/400 →
  extracted `lemma_rt_ref_reduce` + `lemma_rt_negcheck2` → green. Deep
  integration theorem `lemma_batch_body_eq_compress_affine` verifies
  quickly once branches exist.
- **r19 (wiring)**: `lemma_batch_loop_step` composes exec-to-spec bridge +
  deep theorem + doubled-affine identity + byte equality outside the loop;
  loop body = ghost-snapshot every intermediate (`e_nat … magic_nat`,
  `negcheck1_spec`), three `lemma_is_negative_bridge` blocks, one
  `lemma_batch_loop_step(...)` call per iteration; loop invariant carries
  only structural facts. Module `ristretto` green 08:09:44 (first ever in
  arm); whole-crate green 08:16:46 (agent's check, process-paired) and
  08:23:16 (harness gate) → sealed COMPLETE.

**Five decompose-beats-budget instances** were logged in A9 alone (r13
roadmap, r16, r17 split, r18 ×2) — the same pivot the fable arm's
batch-compress saga recorded (rlimit 600→900 timeouts → "single-query body
is too big" → structural extraction). Independent rediscovery at H0; the
strongest in-arm support for the hardness thesis ("difficulty concentrates
where solver cost meets invented decomposition").

## Final proof artifact (shape)

- New lemma home `lemmas/ristretto_lemmas/batch_compress_lemmas.rs`:
  2,641 lines, **33 proof fns, 0 spec fns** (frozen-contract discipline —
  no definitional surface added). Ladder: reusable field/sqrt leaves →
  branch lemmas → one integration theorem → one loop-step lemma.
- `ristretto.rs`: +217/−52 — exec loop refactor (iterator→explicit loop,
  disclosed `ORIGINAL CODE` comments), ghost mirroring, invariant, wiring.
- A9 delta: 2,451+/52− over exactly those 2 files (both editable). FINAL
  patch sha256
  `67e949afa00cfe9c53a38f353941704271c4776780e22f2038166a0958888c21`.
- Cumulative arm mass: A9 seed patch (A1–A8 work) numstat 13,313+/46−
  over 23 files, plus A9 delta 2,451+/52− — simple addition
  **15,764+/98−** (both components dual-verified; the exact
  final-tree-vs-base numstat from `gt_scratch` has not been independently
  derived and this figure should be read as patch-sum, not tree-diff.
  Fable-5 cone analog: 11,024 vs GT's 22,753 removed).

## Certificate battery (A9, bilateral — identical results both agents)

- Forbidden adds AND removes: `admit(` 0, `assume(` 0,
  `external_body` 0, hollow `ensures true` 0, new `axiom_` defs 0,
  spinoff prover 0.
- Harness parser census (`lib/admits.inventory_files`, whole crate src):
  `non_axiom_count=0`, `axiom_count=48`, `okay_for_complete=True`.
- `external_body` attr lines: final 91 == base 91 (and zero in the A9
  diff).
- GT-comment probe on the A9 FINAL patch: 0 fingerprint hits.
- Untracked scratch: exactly 1 file (`curve25519-dalek/err.txt`, 6 lines,
  a `search_semantic.py` usage error via allowed `2>` redirect). Benign.
  (A7 had 24 scratch files; A8/A9 ~zero — the redirect hook working.)
- Fresh-container verify: green (see headline).

## Disclosures (certificate-qualifying, both agents signed)

1. **rlimit footprint**: base ref 5 attrs → final tree 17. A9-new: add
   `rlimit(200)` `lemma_generic_rotate_false`, add `rlimit(400)`
   `lemma_generic_rotate_true`, **raise `RistrettoPoint::compress`
   400→1000** (site pre-carried 400 from A7). Cumulative agent-added
   census (12 new sites + 1 raise, per-file list in the tracking file).
   **Load-bearing gates (fable-style single-attr-removal, module scope,
   run 2026-07-07 UTC on a scratch copy of the sealed tree, per-site JSONs
   in VM1 `rlimit_gates_a9/`): exactly 2 of 13 sites are load-bearing** —
   `ristretto::elligator_ristretto_flavor` `rlimit(200)` (removal → 4
   errors, fn-body + while-loop rlimit) and
   `montgomery::differential_add_and_double` `rlimit(100)` (removal → 2
   errors, fn-body rlimit); both failure signatures are pure
   resource-limit. The other 11 drop cleanly under their module gates,
   including the `compress` `rlimit(1000)` endgame raise (decomposition
   made it unnecessary), both batch-compress lemma attrs (200/400), and
   the `montgomery.rs:521` raise (restores to base 20). Caveat: gates are
   single-removal at module scope (fable-parity convention); joint
   removal of all 11 was not tested. Under this fable-parity
   single-removal/module-scope analysis, the load-bearing budget
   footprint is **two functions** (10× and 5× default).
2. **Two added `assert(false)` contradiction-closers**
   (`lemma_field_inv_nonzero` zero-branch; sqrt-helper `root%p==0`
   branch): proven contradictions inside impossible branches — proof-style
   smell, not integrity drift.
3. **Existing-axiom call deps added in A9**: `axiom_edwards_add_complete`
   ×2, `axiom_invsqrt_a_minus_d` ×1, `axiom_invsqrt_factors_over_square`
   ×2 — all pre-existing trusted floor, no new axiom definitions.
4. **Lower-bound relabels**: all A4/A5 "2" frontier claims relabeled
   truncated-lower-bound (pre-fix); honest series restated above.
5. **Brittle frozen asserts**: `number_theory:342` and `lizard:860`
   flipped red/green under crate-context perturbation during the arm
   (096-witness mechanism confirmed in-arm); both green in the sealed
   tree.
6. **Coset hollow-stub history**: A4 left 1 hollow stub (coset), cleaned
   in A8; disclosed campaign-wide.
7. **GT NL comments**: same ≈5.8k surviving comment lines as the fable
   cut (shared init state) — same visibility caveat applies.
8. **Duplicated whole-crate inefficiency**: A9 r19 ran two overlapping
   crate verifies (agent + gate); harmless but wasteful — harness lesson.

## Incident ledger (arm-scoped)

- **T126 de-novo confabulation (A5, bilaterally sealed)**: agent invented
  a corroborating "verus_check --whole-crate exit 0 … COMPLETE and GREEN"
  memory with zero matching cli.log lines; burned r8–r24 in an
  instant-LIMIT loop. The arm's headline overfitting datum.
- **Transient `assume(false)` (A9 r17)**: left in in-progress scratch,
  self-removed BEFORE any module check ran; no scored result carried it;
  a survivor would have tripped the forbidden-construct counter.
  Overfit/discipline datum, not a gate event.
- **Policy-hook blocks ×2 (A9)**: one background-verifier attempt and one
  stdout-redirected whole-crate attempt blocked by
  `lib/verifier_policy_hook.py`; agent immediately used the allowed form
  both times. Live validation of the ebff7cd fix.
- **Truncation fix validation (A6)**: honest labels moved the agent to
  module-focus from r2 (vs A4's r11) and killed the fake-frontier "2";
  fail-closed also caught a dead num-bigint-drift run pre-arm.
- **Rate-limit warning (A9 r16)**: `seven_day` utilization 0.91
  `allowed_warning` — never saturated; noted for cost accounting.
- **Operator mis-pairing lessons (driver-side, 2 withdrawn claims)**:
  overlapped-cadence verifier results must be paired by process/duration,
  not adjacency (A7 r13, A9 r4 corrections on ledger). The A9 endgame
  crate green was therefore process-paired (`ps` evidence) before posting.

## Verifier-time and cost accounting

Agent wall time: Σ `result.json.duration_seconds` A1–A9 =
**224,205.77 s = 62.28 h** (codex, primary artifacts). Verifier compute
(codex FIFO parse of all A1–A9 `cli.log` command/result pairs — method
stated because it does not reproduce earlier live-snapshot scopes):
whole-crate **207 starts / 200 completed / 110,008.9 s (~30.6 h)**; module
**723 starts / 720 completed / 145,717.7 s (~40.5 h)**. API cost: only
per-attempt recorded figures exist (A1 $69.65, A3 $137.85, A5 ≈$34; some
final-round costs unrecorded — the H2 gap); round JSONs expose
`raw_usage_summary` token counters but no reliable top-level cost, so
**no arm-total dollar figure is published**.

## Comparison to the fable-5 arm (same cut, same harness)

| | fable-5 (096/097) | opus-4.8 (098–106) |
|---|---|---|
| attempts / agent-hours | 2 / 11.4 h | 9 / 62.28 h (Σ result.json durations) |
| first-measured → sealed | 166 → 0 | 166-class start (202 raw) → 0 |
| verified at seal | 2031 (second-machine 2031) | 2114 — same whole-crate Verus counter but NOT the same declaration set (opus tree: 859 `proof fn`/20 `spec fn`; fable tree: 822/19); read as same-counter different-tree, not a productivity metric |
| beyond-GT rlimit at seal | 2×20 within-GT + 3×150 + 4×300; exactly 1 load-bearing | 12 new + 1 raise (incl. compress→1000); **exactly 2 load-bearing** (elligator 200, differential_add_and_double 100); compress-1000 + both batch-compress attrs droppable |
| hardest spot | ristretto batch-compress assembly | same (identical obligation, `ristretto.rs:1731`) |
| resolution mechanism | decompose-beats-budget (agent-initiated) | same, ×5 instances |
| headline pathology | (none of this class) | A5 de-novo confabulated green memory |
| notable harness deltas mid-arm | none | truncation fail-closed; stdout-redirect hook (both dual-signed, user-granted) |

Both arms converged on the same architecture GT used (fine-grained lemma
home + cheap loop; GT pre-paid with a 23-lemma home). Three provers, one
proof engineering lesson, discovered three independent ways.

## Paper-flagged analysis notes (promoted from the tracking file)

- **Two-metric split (both agents, A9 r13)**: at an rlimit wall,
  `verified_count` and frontier-source-span-moved diverge — the bank grew
  +29 units across r9–r18 while the source frontier sat at the same
  2-error wall; r19's final +2 then closed it to 0 / COMPLETE. Report
  both metrics; either alone misstates progress (bank-only overstates,
  frontier-only hides the ladder being built).
- **Reusable-proof-bank / harvest lesson (codex, A9 r13–r16)**: opus spent
  live rounds re-deriving broadly reusable field/sqrt facts
  (diff-of-squares, invsqrt-of-nonzero-square, abs-strip). The seed chain
  preserves them across attempts by construction, but nothing shares them
  across *targets/arms*; a cross-attempt proof bank promoted into
  failure-memory/seed metadata is the identified (not built) harness
  extension. Kept out of the arm per harness-changes-minimal.
- **Efficiency observations (A9)**: the module-check ratchet is the cost
  center — e.g. r17's `lemma_rt_s_match` took ~10 full-module probes to
  teach Verus one algebra equality; r16 spent a 25.8-min round + a
  6.3-min crate gate for +4 scored units; one round opened with ~14k
  thinking tokens before the first action. Decomposition search, not
  proof text, dominates wall-clock.
- **Session auto-reset usage**: resets fired in every multi-round
  attempt (A1 [2,6,8]; A2 [3,4,5]; A3 [4,6,8,14]; A4 [2,4,7,10,11,12,13]
  — reset-cap ended A4; A5 [3,5,8]; A6 [3,5,8,12,16]; A7 [3,4,6];
  A8 [3,5,7]). The A4 seal was reset-cap-bound, the only attempt ended by
  that mechanism.
- **Per-run verifier-start counts** (codex FIFO census, cli.logs):
  whole-crate starts 098=9, A2=18, A3=31, A4=31, A5=28, A6=26, A7=24,
  A8=17, A9=23 (sum 207); module starts 098=64, A2=125, A3=119, A4=64,
  A5=23, A6=113, A7=68, A8=78, A9=69 (sum 723). Late attempts shifted
  from crate-polling to module-focused checking as truncation-honest
  labels landed (A6+) — visible in the A5→A6 module-start jump.
- **Budget claim resolved (2026-07-07 UTC)**: the fable-style load-bearing
  gates were run post-endpoint (see Disclosures #1): 2 of 13 sites
  load-bearing. The earlier 13-site figure is the search-process
  footprint; the proof-artifact footprint is 2 functions.


## Claim boundary

This record claims: the opus-4.8 arm, H0/unassisted on the Stage-3
field-floor cut, reached a harness-sealed, fresh-container-re-verified
whole-crate COMPLETE in 9 seed-chained attempts, with the disclosures
above. It does NOT claim: attr-free convergence (see rlimit footprint),
single-attempt convergence, or cost parity with the fable arm. Lowest
converging hint level: H0 (no operator/GT hint at any point; the
`operator_brief` escalation ladder was never used).
