# Proof-shape analysis: agent vs GT — run 092 (montgomery.rs API strip-all)

> **Claim scope**: run 092 is a Class A rung (GT-neighbored feasibility —
> all 22 lemma homes present as frozen proven GT). This analysis compares the
> *agent-written API proofs* against *GT's API proofs for the same file*,
> which the agent never saw: the container mounted only the sealed peeled
> worktree; GT existed solely in the host-side `gt_scratch_1114160` clone.
> Every convergence below is therefore forced by the frozen contract/floor
> structure; every divergence is an independent proof-engineering choice.

## Inputs

| version | source | lines |
|---|---|---:|
| GT | `git show corefloor-base-103b92b9:curve25519-dalek/src/montgomery.rs` | 2940 |
| peeled (launch state) | sealed worktree `HEAD` (`453e8fd8`), tree-identical to preflight | 1147 |
| agent (sealed final) | 092 worktree after `COMPLETE` seal | 1990 |

GT proof mass stripped by the cut: 2940 − 1147 = **1793 lines** (16 inline
`proof{}` blocks). Agent re-proved the same obligations in **+843 inserted
lines** (~47% of GT's mass), 0 deletions (pure-additive — exec untouched).

## Headline

Same proof skeleton — same two load-bearing axioms
(`axiom_xdbl_projective_correct` / `axiom_xadd_projective_correct`), same
loop invariants, same degenerate-case analysis, 52 of GT's 63 distinct
lemma/axiom callees shared — at less than half the proof text, with a
different organization: GT proves everything inline with exhaustive
intermediate assertions; the agent factors repeated arguments into local
helper lemmas and lets the SMT solver bridge between landmark assertions.

## Per-function comparison (inline proof blocks only)

| function | GT lines | AG lines | GT asserts | AG asserts | GT lemma calls | AG lemma calls |
|---|---:|---:|---:|---:|---:|---:|
| default | 6 | 3 | 2 | 0 | 1 | 1 |
| ct_eq | 28 | 6 | 7 | 0 | 2 | 2 |
| eq | 5 | 0 | 2 | 0 | 0 | 0 |
| hash | 25 | 3 | 9 | 0 | 5 | 1 |
| identity | 30 | 11 | 8 | 0 | 9 | 7 |
| zeroize | 5 | 3 | 1 | 0 | 1 | 1 |
| mul_base | 38 | 35 | 8 | 4 | 4 | 3 |
| mul_clamped | 14 | 0 | 3 | 0 | 0 | 0 |
| mul_bits_be | 458 | 189 | 154 | 42 | 21 | 21 |
| to_edwards | 89 | 83 | 30 | 14 | 14 | 23 |
| elligator_encode | 437 | 159 | 112 | 36 | 51 | 42 |
| conditional_select | 20 | 0 | 12 | 0 | 0 | 0 |
| as_affine | 61 | 28 | 21 | 7 | 4 | 5 |
| differential_add_and_double | 530 | 131 | 152 | 17 | 54 | 28 |
| mul | 47 | 3 | 19 | 0 | 5 | 1 |
| **TOTAL (inline)** | **1793** | **654** | **540** | **120** | **171** | **135** |

(The agent's remaining ~190 added lines are five new local helper `proof fn`s
— see below — whose internal lemma calls are not in this per-function table;
the whole-file recount below includes them.)

Tactic mix (inline blocks): GT `bit_vector` ×4, `reveal` ×10; agent
`bit_vector` ×1, `reveal` ×4; neither uses `nonlinear_arith` or `calc!`.

## Lemma vocabulary (whole-file, helper bodies included)

- GT: 184 lemma/axiom calls over 63 distinct; agent: 165 calls over 64
  distinct; **52 shared**.
- **True GT-only residue** (11, all small utility algebra): 5×
  `lemma_mod_division_less_than_divisor` (agent used `lemma_mod_bound`
  instead), `lemma_inv_mul_cancel` + `lemma_field_mul_assoc` +
  `lemma_mul_by_zero_is_zero` (agent used the packaged
  `lemma_affine_zero_implies_proj_zero`),
  `lemma_projective_implies_affine_on_curve` +
  `lemma_edwards_affine_when_z_is_one` (agent used
  `lemma_valid_extended_point_affine_on_curve` from the *Edwards*
  curve-equation home), `lemma_as_bytes_equals_spec_fe51_to_bytes`,
  `lemma_seq_eq_implies_array_eq`, `lemma_from_u8_32_as_nat`,
  `lemma_as_nat_32_mod_255`, `lemma_pow_nonnegative`, `lemma_mul_basics`,
  `lemma_square_mod_noop`.
- **Agent-only** (12): its 5 new local helpers (below) + 7 frozen-floor/vstd
  lemmas GT never called (`lemma_montgomery_scalar_mul_one`,
  `lemma_mul_mod_noop_right` (vstd), `lemma_field_mul_zero_right`,
  `lemma_affine_zero_implies_proj_zero`,
  `lemma_valid_extended_point_affine_on_curve`,
  `lemma_u8_32_as_nat_of_spec_fe51_to_bytes`, `lemma_canonical_bytes_equal`).

## The invented decomposition (agent's 5 local helper lemmas)

GT's montgomery.rs contains **zero** `proof fn`s — all its helpers live in
the (frozen) lemma homes and everything file-local is proved inline. The
agent added:

1. `lemma_square_output_canonical` (×6 uses) — packages the
   square()-to-`field_square` canonicalization GT re-derives inline 4× via
   `lemma_square_matches_field_square` chains.
2. `lemma_hash_canonical_bytes` — hash-path byte canonicalization.
3. `lemma_bits_be_reversal_value` — the bits-LE→BE value argument (built on
   the same `lemma_bits_*` family GT uses inline).
4. `lemma_consecutive_multiples` — **the standout**: [k+1]B − [k]B = B and
   [k+1]B ≠ [k]B for finite B, proved from scalar-mul succ + add-inverse +
   associativity + identity. A clean group-theory fact GT never names.
5. `lemma_dad_step(B, m, n, …)` — one Montgomery-ladder step at the abstract
   level, parametrized `m == n+1 || n == m+1` so it serves both ladder-case
   ensures. GT proves the two cases inline, ~100 lines each, duplicating the
   representation-lifting and algebra; the agent's two cases collapse to
   3-line `assert forall … by { lemma_dad_step(…) }` invocations. This is
   where most of `differential_add_and_double`'s 530→131 compression comes
   from — deduplication by abstraction, not skipped work (the montgomery
   add/scalar-mul algebra GT calls inline appears verbatim inside the helper
   bodies).

## Style: exhaustive ledger vs landmarks

GT states every intermediate step (540 inline asserts): ~20 field-op
equality asserts per big function, explicit `reveal` + formula expansion for
each spec function, limb-by-limb bounds asserts. The agent states landmark
facts (120 asserts) — e.g. the four final `spec_xdbl/xadd` correspondences —
and lets Z3 bridge. Micro-example: `APLUS2_OVER_FOUR` 51-boundedness takes
GT 15 lines (limb-by-limb + `assert forall` + per-branch `bit_vector`); the
agent does it in one 3-line `assert … by (bit_vector)`. On trivial impls
(`eq`, `mul_clamped`, `conditional_select`) the agent wrote **zero** proof
text where GT spent 5–20 lines each — the whole-crate gate confirms those
obligations still discharge.

## Resource-limit evidence (downgraded 2026-07-03: possibly comment-guided)

GT's `mul_bits_be` carries the comment: *"VERIFICATION NOTE: refactoring
lemma calls into `assert…by` style breaks rlimit"* — and post-hoc audit
shows this comment **survived the peel and was visible to the agent** (the
cut strips proof code, not comments). The agent wrote the denser by-block
style anyway and placed its single budget attr, `rlimit(20)`, on exactly
that function. The peeled baseline independently failed rlimit there at
default, so the obligation *is* resource-marginal — but the "two
independent provers converged" framing is overstated: the agent may have
been guided by GT's note. Report as: obligation verifiably marginal
(baseline evidence); attr placement possibly comment-informed. (Contrast: 094 edwards and 095 scalar closed their baseline
rlimit errors with **no** budget attrs.)

## Exec-immutability note

092's diff is pure-additive (+843/−0). Deletion-side audit across all four
API rungs (093: −9, 094: −1, 095: −4): every deleted line is a blank line or
comment; zero exec statements were changed anywhere. (Policy per user
2026-07-02: exec code must never change; a mechanical erasure-equivalence
audit is part of the Stage-3 bank battery.)

## Paper reading

Under contract pins the model does not replay GT — it re-derives the forced
mathematical skeleton and makes independent, arguably better-factored,
engineering choices at half the proof mass. This simultaneously (a) shows
the reconstruction is genuine proof-engineering rather than recall, and (b)
serves as oracle-leak evidence for the sterility story: an agent copying GT
would not invent `lemma_dad_step`.

## Regeneration

GT: `git -C gt_scratch_1114160 show corefloor-base-103b92b9:curve25519-dalek/src/montgomery.rs`.
Peeled: `git show HEAD:…` in the sealed 092 worktree
(`agents_api_stage2/montgomery_api_092_180_fable/agent_0/work`, VM1).
Agent: the same worktree's working copy. Analysis script (stdlib-only):
extract balanced `proof {…}` blocks per enclosing `fn`, count
lines/asserts/`by(bit_vector|nonlinear_arith|compute)`/`calc!`/`reveal` and
`lemma_*(`/`axiom_*(` calls per block; whole-file vocabulary =
`(lemma|axiom)_[a-z0-9_]+\(` over the full text of each version.
