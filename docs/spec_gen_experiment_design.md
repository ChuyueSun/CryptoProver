# Spec-gen experiment design

Design notes for the **spec-strip / proof-reconstruction** experiments on the
`spec_gen` branch: take a clean, fully-proven curve25519-dalek module, remove
part of its proof, and have a `claude -p` agent reconstruct it against **frozen
specs** — measuring proof-reconstruction capability while *guaranteeing the agent
cannot weaken the user-facing contract*.

Companion docs: `docs/extension_spec.md` (E6 — the experiment mode), the
difficulty-rung table in `docs/website_backend.md`, the **how-to runbook**
`docs/spec_gen_runbook.md` (§1 is peel mode — the canonical, data-driven way to
build these cuts), the rung↔manifest mapping in `peel_manifests/README.md`, and
the per-run reports under `results/<run_id>/.../report.md` (e.g.
`results/no_api_proof_001/edwards/report.md`, which also carries the
codebase-layers diagram).

> **Build-side note.** The cuts described below (which fns to strip/delete/admit,
> what to freeze) are now expressed as **peel manifests** built by `peel.py` — one
> *peel-depth* axis over the proof stack (P1 proofs → P2 lemmas → P3 specs → P4
> contract) with an enforced pin rule. The `demo_decompress.sh` rungs are
> run-side labels for particular cuts, not the canonical vocabulary. This is
> purely the *builder*; the design — layers, anchors, what makes a cut sound —
> is unchanged. See `docs/spec_gen_runbook.md` §1.

---

## Codebase layers (the substrate every cut sits in)

The Verus tree is a dependency stack, top (what users call) to bottom (trusted
floor). Each layer is *proven using the layer below it*.

```
L1  Public exec API     edwards.rs, montgomery.rs, ristretto.rs, scalar.rs
L2  Spec vocabulary     specs/*.rs   ← the contracts are WRITTEN in this
L3  Correctness lemmas  lemmas/edwards_lemmas/, ristretto_lemmas/, …
L4  Field               specs/field_specs*, lemmas/field_lemmas/, backend/…/field.rs
L5  Number theory / vstd  common_lemmas/, vstd   (the assumed floor)
```

(L1 = top, L5 = floor; dependencies flow downward. This is *not* a global total
order — `ristretto.rs` is an L1 API built on top of the `edwards.rs` L1 API.)

A rung **strips one slice and freezes everything else.** The anchor is always a
frozen L1 **contract** written in frozen L2 **vocabulary**; the agent reconstructs
L1 *proofs* and/or L3 *lemmas*; L4/L5 are the assumed floor.

---

## The user-facing API surface (scouted 2026-06; verify before trusting)

This is the key fact for choosing **strip targets** and **contract anchors**: of
all the `pub fn` in the crate, only a minority are genuine user-facing crypto
APIs — the rest are Verus instrumentation, backend internals, and spec helpers.
Only a genuine user-facing API is a clean contract anchor.

### Raw counts (`curve25519-dalek/src`)
| Kind | Count |
|---|--:|
| `pub fn` (exec, public) | **176** |
| `pub(crate)` / `pub(super) fn` | 29 |
| `pub … spec fn` (spec vocabulary — *not* exec) | 366 |
| `pub … proof fn` (lemmas + axioms — *not* APIs) | 785 |

### The 176 `pub fn`, categorized
| Category | ~count | What | User-facing? |
|---|--:|---|:--:|
| Trusted assume-shims | ~51 | `core_assumes.rs` (25: `u64_to_le_bytes`, `try_into_32_bytes_array`…), `subtle_assumes.rs` (26: `select`, `ct_eq_*`, `choice_*`) — `assume`-wrappers modeling std/`subtle` | **No** |
| Backend internals | ~44 | `backend/serial/u64/field.rs` (5), `…/scalar.rs` (16), `backend/mod.rs` (10), `window.rs` (9), `curve_models` (4) | **No** |
| Spec / iterator helpers | ~10 | `specs/iterator_specs.rs` | **No** |
| **Genuine crypto API** | **~65** | the 5 modules below | **Yes** |

### The genuine user-facing API (~65 `pub fn`, 5 modules)
- **`edwards.rs` (17)** — `EdwardsPoint`/`CompressedEdwardsY`: `decompress`,
  `compress`, `to_montgomery`, `mul_base`, `mul_clamped`, `mul_by_cofactor`,
  `is_small_order`, `is_torsion_free`, `vartime_double_scalar_mul_basepoint`, …
- **`ristretto.rs` (19)** — `RistrettoPoint`/`CompressedRistretto`: `decompress`,
  `compress`, `from_uniform_bytes`, `hash_from_bytes`, `from_hash`,
  `double_and_compress_batch`, `random`, `mul_base`, `basepoint`, …
- **`montgomery.rs` (7)** — `MontgomeryPoint`: `to_edwards`, `as_affine`,
  `mul_base`, `mul_clamped`, `mul_bits_be`, …
- **`scalar.rs` (13)** — `Scalar`: `from_bytes_mod_order(_wide)`,
  `from_canonical_bytes`, `invert`, `batch_invert`, `random`, `hash_from_bytes`,
  `from_hash`, …
- **`lizard/lizard_ristretto.rs` (9)** — Lizard encoding on Ristretto:
  `lizard_encode`, `lizard_decode`, …

### Nuance — the `_verus` twins
Many API methods carry a `_verus`-suffixed twin (`multiscalar_mul_verus`,
`hash_from_bytes_verus`, `from_hash_verus`, `double_and_compress_batch_verus`,
`nonspec_map_to_curve_verus`, …): these are the **verification reimplementations**
this fork added — the proofs hang off the `_verus` variant. Folding the twins +
internal helpers (`sum_of_slice`, `create`, `unpack`) out, the *distinct* public
crypto surface is more like **~45**.

### Why this matters for experiment design
- A **valid contract anchor / strip target is a genuine user-facing API** (the
  ~45). The 110+ non-API `pub fn` are not anchors: assume-shims are the trusted
  boundary, backend fns are sub-L1, spec/iterator helpers aren't exec.
- Targets stripped so far — `EdwardsPoint::decompress`, `MontgomeryPoint::to_edwards`,
  and (proposed) `RistrettoPoint::decompress` — sit at the *top* of that list,
  which is exactly why they make clean anchors.
- `spec fn`s (366) are **vocabulary, never targets**: they define what contracts
  mean and must stay frozen (see invariants). `proof fn`s (785) are the
  reconstruction material.

---

## Contract-integrity invariants (hold for every rung)

1. The target's `ensures` is **byte-frozen**, and **every spec fn it is written in
   is frozen and lives OUTSIDE the editable files** — the editable files contain
   **zero** `spec fn`s the contract references. Otherwise the agent could redefine
   the contract's meaning. (You can't weaken what you can't write.)
2. When an editable file is *not* a sibling of the target (e.g. `montgomery.rs`
   holding `to_edwards`), the frozen-file guard no longer protects its contract —
   so `run.py` snapshots **every** `--experiment-allow-edit` file, and the spec
   gate freezes those contracts (`SPEC_DRIFT` on any drift).
3. Lemmas may be **fully deleted** (agent re-derives sig+contract+proof) as long as
   (1) holds — a too-weak re-derived lemma contract can't weaken the guarantee, it
   just fails a frozen consumer → not `COMPLETE`. Never delete an `axiom_*`
   (`AXIOM_DRIFT`) or a `spec fn`.
4. Soundness gates stay on: whole-crate `cargo verus verify`, frozen-file guard
   (`FROZEN_EDIT`), spec gate (`SPEC_DRIFT`), axiom gate (`AXIOM_DRIFT`),
   `GIT_RECOVERY` (reconstruct, don't retrieve originals from history).

---

## Rung ladder (hardest last)

| Rung (`demo_decompress.sh` flag) | Cut | Status |
|---|---|---|
| `bridge-specs` (`--no-bridge-specs`) | reconstruct the deleted Montgomery↔Edwards map spec fns | built |
| `bridge-full` (`--no-bridge-lemmas`) | frozen map; reconstruct the whole decompress L3 lemma layer (proofs only) | built; verified clean |
| `no-api-proof` (`--no-api-proof`) | also strip the two L1 API proof bodies (`decompress`, `to_edwards`); reconstruct all proofs from frozen contracts + specs | built; `no_api_proof_001` COMPLETE in 1 round, indep. 2065/0, both contracts byte-identical to gt, zero weakening |
| `no-ristretto-proof` (`--no-ristretto-proof`) | one layer up: strip the proof bodies of `CompressedRistretto::decompress` + its `step_1`/`step_2` helpers (contracts frozen, no lemma deletion — none are decompress-only); editable = `ristretto.rs` only; frozen edwards/field/ristretto-spec/ristretto-lemma substrate | built; not yet run |
