# Trusted-Core Continuation Record

This record separates the declared experiment scope, the effective starting
state, and the compute lineage of the later trusted-core continuation. Those
three boundaries are different and should not be collapsed into one claim.

## Outcome

The terminal GCP31 segment ended `COMPLETE` with a runner-owned all-zero
whole-crate Verus gate. Its accepted tree receipt is
`4c8c093103e82ef209d4d6ee4f032d317fd66bc6f1da55e8f831f7618c5fbfa4`.
The terminal proof result is valid.

## Declared Scope

The two checked-in manifests give an exact task-surface comparison:

| Manifest | Editable files | Named lemma deletions |
|---|---:|---:|
| `field_floor.json` | 26 | 235 |
| `trusted_core_floor.json` | 86 | 815 |
| Increase | **+60 (3.31x)** | **+580 (3.47x)** |

These figures measure the editable synthesis surface, not the size of the
trusted library. Every field-floor entry is also present in trusted-core.
Trusted-core additionally removes in-repository field/common proof facts and
strips more proof bodies, while external `vstd`, intentional `axiom_*`
declarations, executable code, contracts, and spec definitions remain fixed.

Of the 580 additional named lemma deletions, 428 are in the paper-defined
field/common trusted support: 269 field lemmas, 151 common-arithmetic lemmas,
and 8 proof lemmas colocated with the field specifications, across 35
mixed-content files. The remaining 152 belong to other proof surfaces that
field-floor froze because they were outside its task scope. Thus 428 is the
closest manifest-backed measure of the additional in-repository trusted proof
support, but it is not a complete library-size metric: `vstd` and axioms are
shared by both configurations, spec definitions stay frozen in both, and
stripped proof blocks have no lemma-count entry.

## Effective Starting State

The successful lineage was not launched from a receipted cold trusted-core
peel. GCP14 reset the reused worktree to the field-floor cut. GCP15 then reused
that partially completed worktree while declaring
`trusted_core_floor.json`; the reuse path did not rebuild the peel, and the
legacy worktree had no manifest fingerprint.

The independent field-layer census confirms the consequence: 21 of 27
field-layer files, including both field-specification files and `src/field.rs`,
were byte-identical to the human reference at GCP31 round 0 and remained so at
round 37. The field layer was therefore part of the effective inherited floor.

The defensible result statement is:

> A cumulative continuation verified the package under the declared
> trusted-core scope.

It is not a cold trusted-core completion, and it does not show that CryptoProver
synthesized the human field-specification vocabulary.

## Compute Boundary

The GCP31 terminal segment alone receipted 11:15:53.583 of harness agent time
and $225.7324965. Those are not full-lineage totals.

Following the accounting ruling to count only legitimate resumes from valid
accepted states, the GCP14-GCP31 carried-state ancestry receipts at least
$730.25398925 and 44:14:36.049 of harness agent time. It excludes rejected
GCP22 round 2 and the discarded GCP23 and GCP27 branches. GCP23 and GCP27 have
no terminal billing events, so the amount remains a lower bound with unknown
partial usage omitted.

For completeness, including every branch attempted within GCP14-GCP31 gives a
total-spend lower bound of $740.63396175 and 45:07:50.875. This is a disclosed
campaign-spend variant, not the valid-state-resume headline.

## Provenance

The scope counts above reproduce directly from
[`peel_manifests/field_floor.json`](../../peel_manifests/field_floor.json) and
[`peel_manifests/trusted_core_floor.json`](../../peel_manifests/trusted_core_floor.json).
The effective-floor evidence is summarized in
[`field_layer_agent_vs_gt_gcp31.md`](field_layer_agent_vs_gt_gcp31.md).

The clean public snapshot does not ship raw model streams. The archived primary
sources for this record are `LINEAGE_USAGE_AUDIT.json`, `USAGE_AUDIT.json`,
`FULL_TRACE.md`, and the sealed GCP31 round and promotion receipts.
