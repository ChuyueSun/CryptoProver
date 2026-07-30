# Field-layer census: sealed GCP31 tree vs human ground truth

> **Claim scope**: file-level and lemma-level comparison of the 27 field-layer
> files in the GCP31 sealed final tree (`4c8c0931…`, dual-countersigned,
> round_37_agent snapshots hash-verified against the receipt) against the
> campaign's true ground-truth base `ChuyueSun/dalek-lite@103b92b9`
> (`corefloor-base-103b92b9` — see **Correction 2**; the original analysis used
> `main`/`d74d6892` per diagnostics.md). Analysis 2026-07-19, re-based
> 2026-07-29, mac-local, from
> `results/campaign_traces/peel_corefloor_006_gcp31_resume/full-artifacts.tar.gz`.

> **Correction 2 (2026-07-29, ledger T136; independently countersigned).** The
> peeled baseline `637ff753` was blob-hash-proven to be built from
> `corefloor-base-103b92b9`, NOT `main`/`d74d6892` (all 26 pre-image blob
> hashes of `field_floor_start_state.diff` match `103b92b9`; its post-images
> match the archived GCP14 `snapshots/round_0` 26/26). Re-basing this census
> onto the true reference removes two files from the "agent delta" set —
> `backend/serial/u64/field.rs` and `sqrt_m1_lemmas.rs` differ between the two
> human branches and are byte-identical to `103b92b9` in the sealed tree — and
> reattributes three field_algebra lemmas plus the `lemma_neg_neg →
> lemma_field_neg_neg` rename to the humans. All numbers below are the
> corrected, receipt-verified values (round_37 snapshots re-hash-verified
> against the receipt 87/87 during both recomputations).

## Headline

**The field layer of the sealed tree is overwhelmingly the human text, not
agent regeneration.** 23 of 27 field-layer files (count corrected twice: 20→21
per codex's independent recomputation, T127 2026-07-19; 21→23 per the re-based
reference, Correction 2) — including both spec files (`field_specs.rs`,
`field_specs_u64.rs`), `src/field.rs`, and `backend/serial/u64/field.rs` — are
**byte-identical to GT at GCP31 round 0 and still at round 37**. The
`trusted_core_floor.json` manifest *declares* 277 field-layer lemma deletions,
but the executed chain never performed that peel: GCP14 reset gcp8 to the
**field-floor** cold cut (field layer = floor, kept), and GCP15+ reused that
worktree under the trusted-core declaration without re-peeling (the receipted
seam behind the "no cold trusted-core start" retraction, mvp ledger T127
2026-07-18). The field layer is therefore part of the **effective floor** of
the executed campaign.

Consequences for any prose:

- "The agent synthesized the field specifications" is **false** for the
  executed chain. The field spec vocabulary is 100% human.
- Signature-fidelity numbers over the 277 manifest-listed field lemmas
  (~272/277 identical by parse) **must not** be read as regeneration fidelity —
  those lemmas were never deleted in the run that executed.
- This is *consistent* with the paper's main field-floor framing (field
  specs/arithmetic facts are trusted-floor inputs there by design), and it
  concretely reinforces the ruled wording: "cumulative continuation under the
  declared trusted-core scope," never a trusted-core-completion claim.

## Genuine agent field-layer work (delta vs GT in the sealed tree)

**+320/−5 lines across 4 files; 7 new lemma statements; 0 renames; 0 spec-fn
changes; 0 axiom changes** (corrected from ~+451/−47 across 6 files / 10 new
names — see Correction 2; the removed portion was human branch drift between
`main` and `corefloor-base`). Only `field_algebra_lemmas.rs` also changed
*during* GCP31 (r0 ≠ r37); the rest was inherited from earlier arms of the
chain.

| file | +/− vs GT@103b92b9 | agent-new lemma statements / changes |
|---|---|---|
| `field_algebra_lemmas.rs` | +185/−0 | 4 new: `lemma_diff_of_squares_zero_when_prod_zero`, `lemma_proj_u_zero_implies_prod_zero`, `lemma_square2_matches_two_field_square`, `lemma_edwards_to_montgomery_u` |
| `sqrt_ratio_lemmas.rs` | +67/−0 | 2 new: `lemma_is_sqrt_ratio_one_implies_square`, `lemma_is_sqrt_ratio_times_i_one_implies_nonsquare` |
| `as_bytes_lemmas.rs` | +67/−5 | 1 new: `lemma_canonical_bytes_of_from_bytes` (byte round-trip used by ristretto decompress) |
| `limbs_to_bytes_lemmas.rs` | +1/−0 | signature strengthening of `lemma_limb0_contribution_correctness` |

Reattributed to the humans by Correction 2 (present in `103b92b9`, so branch
drift, not agent work): `lemma_one_and_neg_one_square_to_one`,
`lemma_affine_zero_implies_proj_zero`, `lemma_field_inv_neg`, the
`lemma_neg_neg → lemma_field_neg_neg` rename (and its `sqrt_m1_lemmas.rs`
callee fix), and the `backend/serial/u64/field.rs` two-line delta.

These seven statements are genuine agent-authored *internal specifications* at
the field layer — invented to support the above-floor Ristretto/Montgomery
endgame, then proved and consumed by fixed callers.

## Caveats

- Diffs are measured against `103b92b9` (`corefloor-base`), the blob-hash-
  proven source of the peeled base `637ff753` (Correction 2) — the previously
  open source-fork caveat is now CLOSED. The seven new lemma names are
  agent-era (absent from GT at the true base, referenced by GCP31's own round
  diffs).
- Parser granularity: lemma inventory via `proof fn`/`spec fn` regex + brace
  matching; byte-identity comparisons are exact and parser-independent.
- **Where real spec regeneration happened**: the above-floor layers
  (Edwards/Montgomery/Ristretto/scalar lemma files), where the executed
  deletions and the 191→0 receipted frontier live. The meaningful
  agent-vs-human internal-specification comparison for the paper is over those
  files, not the field layer.
