# Verus Spec Vocabulary — curve25519-dalek

All names below live under `crate::specs::*` (mostly
`specs/edwards_specs.rs`, `specs/field_specs.rs`, `specs/scalar_specs.rs`,
`specs/montgomery_specs.rs`). Run
`python skills/search_module.py "crate::specs::edwards_specs" --project <root>`
to see the current contents — this file is a curated index, not the
source of truth.

## Representation predicates (use in `requires` and `ensures`)

| Predicate | Type guarded | Notes |
|---|---|---|
| `is_well_formed_edwards_point(p)` | `EdwardsPoint` | All four field limbs within fe51 carry margin; preferred over `is_valid_edwards_point` for top-level specs |
| `is_valid_edwards_point(p)` | `EdwardsPoint` | Stricter — full canonicality; use for outputs that must round-trip |
| `is_valid_extended_edwards_point(x,y,z,t)` | 4 nats | Use in spec-level lemmas where you have already lifted to nat |
| `is_valid_projective_point(p)` | `ProjectivePoint` | Z field limb valid |
| `is_valid_completed_point(p)` | `CompletedPoint` | T1·T2 == X·Y (mod p) |
| `is_valid_affine_niels_point(n)` | `AffineNielsPoint` | Lookup-table entries |
| `is_valid_projective_niels_point(n)` | `ProjectiveNielsPoint` | Lookup-table entries |
| `is_valid_montgomery_point(p)` | `MontgomeryPoint` | U coord valid |
| `is_valid_montgomery_affine(p)` | `MontgomeryAffine` | |
| `is_canonical_scalar(s)` | `Scalar` | `scalar_as_nat(s) < L` (group order) |
| `is_canonical_scalar52(s)` | `Scalar52` | Limbs < 2^52 |
| `is_valid_naf(naf, w)` | `Seq<i8>` | NAF coefficients in (-2^(w-1), 2^(w-1)), odd entries only, sparsity rule |
| `is_valid_radix_16(digits)` | `[i8; 64]` | Each in [-8, 8] |
| `is_valid_radix_2w(digits, w, n)` | `[i8; 64]` | Generalized; check defn for exact bound |
| `is_valid_edwards_basepoint_table(t, B)` | `EdwardsBasepointTable` | Used in vartime double-scalar work |
| `is_valid_lookup_table_affine<N>(...)` | window tables | |
| `is_valid_lookup_table_projective<N>(...)` | window tables | |
| `is_valid_naf_lookup_table5_affine(...)` | 5-bit NAF lookup | |
| `is_valid_naf_lookup_table8_affine(...)` | 8-bit NAF lookup | |

## Lifting functions (exec → spec)

| Function | Result type | When to use |
|---|---|---|
| `edwards_point_as_affine(p)` | `(nat, nat)` | Top-level functional-correctness specs |
| `edwards_point_as_nat(p)` | `(nat, nat, nat, nat)` | When you need projective (X,Y,Z,T) |
| `edwards_y_nat(p)`, `edwards_z_nat(p)` | `nat` | Single-coord access |
| `projective_point_as_affine_edwards(p)` | `(nat, nat)` | |
| `projective_point_edwards_as_nat(p)` | `(nat, nat, nat)` | |
| `completed_point_as_affine_edwards(...)` | `(nat, nat)` | |
| `completed_point_as_nat(...)` | `(nat, nat, nat, nat)` | |
| `affine_niels_point_as_affine_edwards(n)` | `(nat, nat)` | |
| `projective_niels_point_as_affine_edwards(n)` | `(nat, nat)` | |
| `montgomery_point_as_nat(p)` | `nat` | |
| `fe51_as_nat(f)` | `nat` | Field element lift |
| `limbs52_as_nat(limbs)` | `nat` | Raw 5-limb lift |
| `scalar_as_nat(s)` | `nat` | Scalar lift; used in bound idioms |
| `u8_32_as_nat(bytes)` | `nat` | 32-byte little-endian lift |
| `bits_as_nat(bits)`, `bits_be_as_nat(...)` | `nat` | Bit-sequence lift |
| `bytes_seq_as_nat(s)`, `bytes_as_nat_prefix(...)` | `nat` | Byte-sequence lift |

## Affine curve math (spec-level operators)

| Function | Signature | Use |
|---|---|---|
| `edwards_add(x1, y1, x2, y2)` | 4 nats → `(nat, nat)` | Affine point addition |
| `edwards_double(x, y)` | 2 nats → `(nat, nat)` | Affine point doubling |
| `edwards_neg((x, y))` | tuple → tuple | Negation |
| `edwards_sub(x1, y1, x2, y2)` | 4 nats → `(nat, nat)` | Subtraction (= add + neg) |
| `edwards_scalar_mul(P_affine, k)` | tuple + nat → tuple | Scalar multiplication |
| `edwards_scalar_mul_signed(P_affine, k)` | tuple + int → tuple | Variant for signed scalars |
| `edwards_identity()` | → `(0, 1)` | Neutral element |
| `spec_ed25519_basepoint()` | → `(nat, nat)` | Fixed Ed25519 basepoint |
| `edwards_decompress_from_y_and_sign(y, s)` | nat + u8 → Option | Point decompression spec |

## Standard bound idioms

| Idiom | Meaning |
|---|---|
| `scalar_as_nat(s) < pow2(255)` | Standard Ed25519 scalar bound (group order ~ 2^252 + δ, but the codebase uses pow2(255)) |
| `fe51_as_nat(f) < pow2(255) - 19` | Canonical field element (= prime p) |
| `is_well_formed_edwards_point(*A)` | Standard input-point requires clause |
| `forall|i| 0 <= i < 5 ==> f.0[i] < pow2(54)` | 5-limb fe51 carry-margin bound (use `is_well_formed_*` when possible instead) |

## Decreases idioms

For exec loops over a bounded counter `i`: `decreases <N> - i` where `N`
is the upper bound. For recursive proof fns over Seq: `decreases s.len()`.
For divide-and-conquer: include both branches in the tuple,
`decreases s.len(), depth`.

## Where to find more

```bash
# Full list of spec fns in a module:
python skills/search_module.py "crate::specs::edwards_specs" --project <root>
python skills/search_module.py "crate::specs::scalar_specs"  --project <root>
python skills/search_module.py "crate::specs::field_specs"   --project <root>

# Find a predicate by keyword (e.g. "lookup table"):
python skills/search_semantic.py "lookup table valid" --project <root>

# What macros expand to (lemma_* families):
python skills/search_macro.py --name-prefix lemma_edwards --project <root>
```

## Adding new vocabulary

Don't, unless the experiment explicitly calls for it. New spec fns
change the eval surface and risk being "spec invention disguised as
spec inference." If a predicate or lift you need doesn't exist, that
is almost certainly a signal that you are mis-modelling the problem —
re-read the anchor and look for a simpler decomposition first.
