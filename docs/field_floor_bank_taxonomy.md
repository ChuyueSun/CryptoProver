# Field-Floor Bank Taxonomy

This note classifies field-floor integration results so lane-local progress does
not get mistaken for repeatable convergence. It is a runbook until a reviewed
harness schema exists.

## Blocking rule

Every classification change needs three things before it is accepted:

1. Primary artifact evidence.
2. An independent cross-check by another agent or a second command/source.
3. A signed review-ledger note naming both.

For profiler conclusions, record the exact worktree/ref, command/env,
start/end time, raw output artifact, and a second check against logs or result
summaries.

## Classes

### `proof_delta`

A real proof delta has:

- zero non-axiom admits in the claimed target or lane scope;
- no new `admit()`, `assume(...)`, `#[verifier::external_body]`, or
  `proof fn axiom_*`;
- a local/module verifier signal for the edited proof surface;
- an explicit list of remaining integration blockers.

This is useful and bankable as evidence, but it is not `BANKED_COMPLETE`.

### `partial_lane`

Use this when a run proves only part of its editable lane scope. A module-local
green in one edited file does not make the whole lane a `proof_delta` if other
editable files still have live source errors, missing proofs, or remaining
admits.

Separate the blockers:

- `in_scope_incomplete`: unfinished obligations in the editable lane files;
- `off_lane_debt`: frozen or other-lane consumers that still fail and must be
  handled by another lane or integration pass;
- `diagnostic_masking`: a verifier setting that hides the real error set, such
  as a broad high-rlimit whole-crate check timing out before source errors are
  reported.

Current checked example: `057` Montgomery is `partial_lane`, not a clean
`proof_delta`. Phase A on sealed `057` completed at default rlimit with source
errors instead of timing out. The agent had proven
`montgomery_reduce_lemmas.rs` locally, but the editable scope also included
`montgomery_reduce_part1_chain_lemmas.rs`,
`montgomery_reduce_part2_chain_lemmas.rs`, and `scalar.rs`; `scalar.rs` still
had many in-scope source errors. Frozen consumers also produced off-lane errors.
The previous global `--rlimit 80` whole-crate gate masked those real errors as a
900s timeout. Do not classify `058` by analogy without its own artifact or an
explicit cross-check.

### `abi/skeleton_debt`

Use this when a proof delta cannot reach de-stubbed whole-crate truth because
the integration surface is missing generated signatures, contracts, module
skeletons, or prerequisite proof ABI. Operator stubs can hide this boundary, so
the de-stubbed gate must report the actual missing dependency.

Current example: `061-1` coset is a real proof delta with zero target admits,
but the clean de-stubbed bank fails after the strip syntax fix on missing
generated lemma ABI/deps. It is reusable proof signal, not whole-crate complete.

### `whole_crate_gate_timeout`

Use this when the target proof delta is admit-free locally, but the de-stubbed
whole-crate gate times out. Do not label this as cumulative crate cost without
a profiling artifact that proves the frozen crate dominates the time.

Phase A is the required discriminator for a timeout:

- run sealed `057` strict/default whole-crate, with no global `--rlimit 80`;
- if it completes green, global high rlimit was the blocker and the fix is a
  tiered/scoped rlimit gate;
- if it times out, a real divergent Montgomery VC needs proof decomposition;
- if it returns source errors, work those errors.

The checked `057` Phase A took the third branch: default whole-crate returned
source errors. That means the earlier `--rlimit 80` timeout was diagnostic
masking, not proof completion and not by itself evidence of a divergent VC.

## `BANKED_COMPLETE`

Reserve this label for de-stubbed whole-crate truth:

- whole-crate verification succeeds or an approved equivalent chunked replay
  succeeds over the current source and current dependency interfaces;
- full gate-scope non-axiom admit inventory is zero;
- spec, axiom, tooling, forbidden-construct, and frozen-file gates are clean;
- no result depends on a stale cache entry whose imported generated-lemma
  contract/interface changed.

Module green alone is never enough. A local green with residual admits is
admit-masked signal, not a bank.
