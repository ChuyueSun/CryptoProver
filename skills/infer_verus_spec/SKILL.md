---
name: infer-verus-spec
description: Reconstruct Verus fn-header specs (requires/ensures/decreases) from Rust code in curve25519-dalek. Use when a fn has stripped or missing fn-header clauses and the agent must derive them from an anchor (higher-level pub fn whose spec is fixed), the call chain, and the dalek-lite spec vocabulary (well-formedness predicates, as_nat/as_affine lifts, affine curve math). Not for proof-body construction (loop invariants, assert-by, ghost code) — that is separate work.
---

# Infer Verus Spec from Rust Code (crypto)

## Quick start

You are given a Rust file where some fns have `fn name(...) -> T {` with no
`requires`/`ensures`/`decreases` between signature and body. Your job is
to add them back so the existing anchor's proof verifies. The anchor is a
higher-up `pub fn` whose spec is fixed (do not modify it).

Work **top-down** from the anchor: every dep fn's spec is constrained by
what its caller's proof body needs from it. **Spec inference is not
invention** — in most dalek-lite call chains it reduces to transcription
plus picking the right vocabulary.

## Process

1. **Read the anchor's `ensures`.** It states the client-visible
   correctness contract. Common shape:
   ```
   ensures
     is_well_formed_edwards_point(result),
     edwards_point_as_affine(result) == { <affine curve math> },
   ```
   The first conjunct is the representation invariant; the second is
   functional correctness lifted into `nat`-affine math.

2. **Trace the call chain.** Open the anchor's body. For each fn it
   calls into the stripped files, that callee's `ensures` must be strong
   enough for the anchor's body to discharge its own `ensures`. Repeat
   recursively to the leaves.

3. **Classify each link** before writing anything:

   - **Forwarding wrapper** — body is a single `crate::backend::X(...)`
     or `self.inner.X(...)` call. The callee's spec is **forced**:
     copy the caller's `requires`/`ensures` verbatim, rename params.
     Most dispatcher/impl layers in `backend/` are this case.

   - **Internal worker** — body has loops, conditionals, real compute.
     The `ensures` is what the body achieves *in spec language*:
     e.g. a NAF accumulation says `as_affine(result) ==
     edwards_scalar_mul(...) + edwards_scalar_mul(...)`. The
     `requires` are the input bounds the body assumes (read off the
     unchecked indexing, subtractions, etc.).

   - **Leaf lemma** (`proof fn`, no exec body) — statement comes from
     its callsites. Find every `<name>(...)` call in the proof bodies
     of the impl, look at the proof obligation it discharges, and
     write `requires` / `ensures` to bridge.

4. **Pick representation predicates and lifts from the vocabulary.**
   See [REFERENCE.md](REFERENCE.md) for the complete table — common
   ones:
   - Point well-formed: `is_well_formed_edwards_point`,
     `is_valid_projective_point`, `is_valid_completed_point`
   - Lifts: `edwards_point_as_affine(p): (nat, nat)`,
     `fe51_as_nat(f): nat`, `scalar_as_nat(s): nat`
   - Affine math: `edwards_add`, `edwards_double`, `edwards_scalar_mul`,
     `edwards_neg`, `edwards_identity`, `spec_ed25519_basepoint`
   - Scalar bound idiom: `scalar_as_nat(s) < pow2(255)`

5. **Verify locally before claiming done.** Run `verus_check` on the
   anchor **and** on each dep file separately
   (`--verify-module` is module-scoped, so anchor passing does not
   mean dep bodies prove their new ensures).

## Decision rule for tricky cases

- **If anchor passes but dep file fails** → your dep `ensures` is too
  strong for the dep body. Weaken to what the body actually achieves.
  Re-derive from the body, not from "what would be nice."
- **If anchor fails but dep file passes** → your dep `ensures` is too
  weak for the caller. Strengthen, or add a missing conjunct.
- **If you cannot derive a leaf lemma's statement from callsites** —
  the callsites have probably been stripped too. Stop and report;
  inventing a statement that "feels right" wastes rounds.

## Anti-patterns (each will fail verus or fail review)

- Don't modify the anchor — it is the fixed point of the experiment.
- Don't add `#[verifier::external_body]`, `admit()`, or `assume(...)`
  to make a spec "work."
- Don't overspecify (e.g. pin concrete limb values when the spec-nat
  is what callers need). Overstrong `ensures` are unprovable from the
  body.
- Don't underspecify input bounds. Callers see "precondition not
  satisfied" if `requires` is missing scalar/limb bounds the body
  uses.
- Don't invent novel spec math (new `spec fn`s). If the anchor doesn't
  ground the operation in existing vocabulary, you cannot derive it.

## Out of scope

This skill is **only for fn-header specs**. Loop invariants, `assert(...)
by (...)`, `proof { ... }` blocks, ghost bindings, and helper-lemma
proof bodies are a separate concern. After header specs land and the
anchor verifies, the dep body proof is a distinct (and harder)
workflow.

## Related skills in this repo

- `skills/verus_check.py` — source of truth for "did the spec
  type-check and prove."
- `skills/search_semantic.py` — find existing spec fns / predicates
  by keyword.
- `skills/search_module.py` — list everything in `specs::edwards_specs`,
  `specs::scalar_specs`, etc. before inventing a new predicate.
