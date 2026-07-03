# GT-divergence study: the certificate cone (097) vs ground truth

> Companion to `proof_shape_092_agent_vs_gt.md` (single-file case study) —
> this is the same question at whole-cone scale: **did the certificate agent
> replay GT, or invent?** The agent never saw GT's cone (sterility gates,
> verified at both launches); name overlap below is therefore either forced
> by frozen-callsite pins or convergent.

## Per-home lemma architecture (GT vs agent, sealed 097 worktree)

| home / file | GT lemmas | agent | shared names | GT lines | agent lines |
|---|---:|---:|---:|---:|---:|
| curve_equation_lemmas.rs | 49 | 30 | 19 | 3296 | 1287 |
| straus_lemmas.rs | 30 | 29 | 19 | 1322 | 1281 |
| **batch_compress_lemmas.rs** | **23** | **12** | **0** | 2947 | 2003 |
| mul_base_lemmas.rs | 16 | 17 | 4 | 1177 | 836 |
| montgomery_reduce_lemmas.rs | 16 | 12 | 12 | 1202 | 773 |
| pippenger_lemmas.rs | 15 | 18 | 6 | 1316 | 1005 |
| radix_2w_lemmas.rs | 15 | 18 | 3 | 1334 | 905 |
| niels_addition_correctness.rs | 9 | 6 | 5 | 1526 | 778 |
| scalar_to_bytes_lemmas.rs | 9 | 11 | 2 | 1362 | 739 |
| naf_lemmas.rs | 9 | 7 | 3 | 653 | 317 |
| **torsion_lemmas.rs** | **8** | **0** | 0 | 464 | 84 |
| constants_lemmas.rs | 7 | 8 | 4 | 332 | 253 |
| step1_lemmas.rs | 6 | 2 | 0 | 572 | 204 |
| decompress_lemmas.rs | 5 | 3 | 0 | 356 | 295 |
| montgomery_reduce_part1_chain.rs | 4 | 3 | 1 | 914 | 351 |
| radix16_lemmas.rs | 4 | 5 | 3 | 273 | 205 |
| bytes_to_scalar_lemmas.rs | 3 | 3 | 3 | 761 | 235 |
| vartime_double_base_lemmas.rs | 2 | 5 | 2 | 112 | 187 |
| **montgomery_reduce_part2_chain.rs** | **2** | **0** | 0 | 626 | 56 |
| double_correctness.rs | 1 | 1 | 1 | 290 | 246 |
| coset_lemmas.rs | 1 | 2 | 1 | 153 | 233 |
| elligator_lemmas.rs | 1 | 1 | 0 | 55 | 88 |
| APIs (mont/rist/edw/scalar, local helpers) | 0 | 3 | 0 | 18285 | 15238 |
| **TOTAL** | **235** | **196** | **88** (same-home) | — | — |

(API line counts include frozen exec/contracts; the editable proof deltas
are in the evolution doc. Sealed-cone totals — method: `git diff --no-index
--numstat` over the 26 editable files, exported `corefloor-base-103b92b9`
vs sealed 097 worktree: **+11,024 / −22,753 = 48.5% of GT's proof mass**,
83% of its lemma count. Verified independently by both agents.)

## The three-way split of the agent's 196 lemmas

- **88 share GT's name in the same home** — dominated by frozen-callsite
  pins (the pin census's 124 call instances; e.g. `montgomery_reduce_lemmas`
  is 12/12 shared because every GT name there is demanded by a frozen
  caller) plus convergent naming under shared spec vocabulary. A further
  **12 GT names were reused in a *different* home** (global name
  intersection 100) — the agent kept a pinned/convergent name but
  relocated the lemma.
- **108 are pure inventions** with no GT counterpart
  (`lemma_ed0_closed_form`, `lemma_batch_hg_identity`,
  `lemma_edwards_add_rearrange_4`, …).
- **147 GT lemmas were never reconstructed** — GT decompositions the
  agent's architecture simply doesn't need.

## Findings

1. **Freedom yields novelty; pins yield conformance — cleanly separable.**
   Where frozen callers dictate names, overlap is total (montgomery_reduce
   12/12, bytes_to_scalar 3/3); where the agent was architecturally free,
   overlap collapses (batch_compress **0/12**, step1 0/2, decompress 0/3,
   elligator 0/1). The overlap map is essentially a picture of the pin
   census.
2. **The agent quotients the architecture.** Two GT homes were emptied
   entirely (torsion 8→0, part2_chain 2→0) — their consumers' obligations
   were discharged by other routes. The hardest home (batch_compress) was
   fully re-architected: half the lemma count, zero shared names, closed
   after the budget→decomposition arc.
3. **Half the mass, everywhere.** Per-home line ratios cluster around
   0.4–0.7 — the 092 case-study ratio (47%) replicates home-by-home, not
   just in aggregate.
4. **Oracle-independence evidence**: an agent copying GT would not produce
   0/12 name overlap in the largest free home, empty two GT homes,
   relocate 12 pinned names, or skip
   147 of GT's 235 decompositions. Combined with the sterility gates, the
   divergence pattern is the behavioral half of the no-oracle argument.

Regeneration: `gt_divergence_097.py` (verifier-session scratchpad; inputs =
`gt_scratch_1114160` @ `corefloor-base-103b92b9` + sealed 097 worktree /
`stage3_evidence_backups/097_certificate_064133Z`).

## Disclosed caveat: GT natural-language comments survived the cut

The peel removes proof *code* (and docs attached to deleted lemmas), not
comments generally: the peeled launch state retains **≈5.8k GT comment
lines across the 26 editable files** (block-aware count; dual-verified) —
module docs describing deleted lemmas' subject matter, section banners
(some carrying mathematical identities, e.g. batch_compress's
"h² − g² = −e²·(1+d)"), and exec-adjacent VERIFICATION NOTEs. Raw-stream
analysis shows these were **visible and consulted**: assistant-role echoes
of "VERIFICATION NOTE" (8), "breaks rlimit" (2), "h²" (10), "Curve
equation identity" (2) across 096/097; the torsion module docs were
visible with no observed assistant echo. Measured, disclosed hint channel
(applies to Class A identically). It does not weaken this study's
conclusion — the batch_compress banners were consulted, yet the agent's
architecture there shares 0/12 names with GT — but claim wording
campaign-wide is "no GT *proof code* visible", not "no GT content".
