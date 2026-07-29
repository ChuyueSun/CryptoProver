# Field-Layer Census: Sealed GCP31 Tree vs. Human Reference

This is a file- and lemma-level comparison of the 27 field-layer files in the
sealed GCP31 tree against the documented human reference
`ChuyueSun/dalek-lite@d74d6892`. Round snapshots were hash-checked against the
sealed GCP31 receipt.

## Headline

The field layer is overwhelmingly inherited human text, not agent
regeneration. **21 of 27 field-layer files** are byte-identical to the human
reference at both GCP31 round 0 and round 37. Those files include
`field_specs.rs`, `field_specs_u64.rs`, and `src/field.rs`.

The trusted-core manifest declares field-layer deletions, but the executed
lineage never performed that cold peel: GCP14 started from a field-floor cut,
and later segments reused that worktree under the trusted-core declaration.
The field layer therefore remained part of the effective inherited floor.

Consequences:

- The executed lineage did not synthesize the human field-specification
  vocabulary.
- Manifest scope must not be presented as proof that every declared deletion
  happened in the starting bytes.
- The result is a continuation under the declared trusted-core scope, not a
  cold trusted-core completion.

## Genuine Agent Field-Layer Delta

The sealed tree differs from the human reference by approximately +451/-47
lines across six files. It contains ten new lemma statements, no changed spec
function, and no changed axiom. Only `field_algebra_lemmas.rs` changed during
GCP31 itself; the other differences were inherited from earlier segments.

| File | Delta vs. human reference | Agent-era statement change |
|---|---:|---|
| `field_algebra_lemmas.rs` | +313/-41 | Seven new lemmas; one lemma renamed |
| `sqrt_ratio_lemmas.rs` | +67/-0 | Two new lemmas |
| `as_bytes_lemmas.rs` | +67/-5 | One new byte-round-trip lemma |
| `limbs_to_bytes_lemmas.rs` | +1/-0 | One signature strengthening |
| `sqrt_m1_lemmas.rs` | +1/-1 | One callee-rename repair |
| `backend/serial/u64/field.rs` | +2/-0 | Two added lines |

The ten new statements support the above-floor Ristretto and Montgomery proof
endgame and were proved before use. They are genuine agent-authored internal
specifications; they do not change the inherited field-specification
vocabulary.

## Caveat

The byte comparison uses `d74d6892`, the reference pinned by the experiment
diagnostics. The peeled base `637ff753` belongs to the experiment fork. Its
exact relation to `d74d6892` should be established before making paper-grade
per-line authorship claims. The byte-identity result itself is
parser-independent.
