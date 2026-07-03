# How proofs work in the GT corpus (dalek-lite proof style)

A **positive playbook** for an agent (or human) writing Verus proofs against the
`curve25519-dalek` / dalek-lite codebase, distilled from the published
ground-truth repo. This is the companion to [`diagnostics.md`](diagnostics.md):
diagnostics is *symptom → detection → remedy* for failed runs; this doc is *how
the canonical proofs are actually built when they succeed*. When a diagnosis
turns on "is the agent's approach canonical?" (notably
[diagnostics Pattern 3](diagnostics.md), Z3 rlimit on nonlinear arithmetic),
this is the reference for what "canonical" means here.

## Evidence base & how to read the numbers

Drawn from the GT repo's own proof-guidance notes
(`.codex/skills/verus-proof-helper/references/*` + `verus-spec-helper`, mirrored
under `.claude/skills/`) **plus** a raw pull of **56 of 87 `lemmas/` + `specs/`
source files (~11k LOC)** from `Beneficial-AI-Foundation/dalek-lite@main` into a
scratch corpus. Idiom counts below are over that sample.

**Two "GT" refs, kept distinct.** This *style corpus* is sampled from **upstream
`Beneficial-AI-Foundation/dalek-lite@main`** — the right source for general
proof-style idioms, which are shared. The *experiment starting states*, however,
are built from the **`ChuyueSun/dalek-lite` fork pinned at `d74d6892`** (see
[diagnostics.md](diagnostics.md) "Ground-truth source"), and some specific
mechanics referenced here (e.g. the `lemma_unfold_edwards` location) were
confirmed against the experiment/`corefloor` ref. So: trust this doc for *how
the corpus proves*; when you need the exact *answer* to a specific cut, check it
against the pinned fork/corefloor ref, not upstream main.

**Read the counts as ratios, not laws.** Two caveats, both load-bearing:
1. **Sample, not census.** ~31 lemma/spec files weren't pulled, and the counts
   were not validated against a full-tree census. A count of `0` means *"not
   observed in the GT lemma sample, and not canonical for the case at hand"* —
   **not** "this construct is banned." The repo docs themselves still list
   `calc!` as a technique and know about vstd `broadcast` groups.
2. **Two layers.** This corpus is the **proof-library layer** (`lemmas/` +
   `specs/`): pure `proof fn` lemmas and `spec fn` definitions. It deliberately
   does **not** include the **exec-method-proof layer** — the `ensures`/proof
   blocks attached to executable methods in `backend/serial/.../*.rs`. Several
   idioms that look "absent" below (`external_body`, `use_type_invariant`, the
   `/* ORIGINAL CODE */` refactor tag) are real and load-bearing in that second
   layer; they're just out of frame for a lemma-library sample. Where that
   matters, it's flagged inline.

---

## 1. It's a lemma-library codebase, not a tactic codebase

The defining habit: a hard goal is **decomposed into named, reusable lemmas**,
and each step is discharged by citing a lemma inside its own
`assert(...) by { ... }` block — rather than thrown whole at the solver.

> In the sample: **366 `) by {` blocks against 606 `assert(`** — ~60% of
> assertions carry an explicit proof block, overwhelmingly a single lemma call.

The unit of progress is **"find or write the right lemma,"** not "find the right
tactic." First move on any goal: *search before you write.*

## 2. Lemma placement is strict (and is itself a search index)

- `lemmas/common_lemmas/` — the domain-agnostic toolbox: `mul_lemmas`,
  `pow_lemmas`, `div_mod_lemmas`, `bit_lemmas`, `to_nat_lemmas`,
  `bits_as_nat_lemmas`.
- Domain folders build on top: `field_lemmas/`, `edwards_lemmas/`,
  `ristretto_lemmas/`, `scalar_lemmas_/`, `montgomery_*`.

The skill notes encode a **placement table** (generic field algebra →
`field_algebra_lemmas.rs`; Ed25519 curve structure → `curve_equation_lemmas.rs`;
decompression → `decompress_lemmas.rs`). The rule that follows from it:

- **Search order:** same file → `common_lemmas` → domain folder.
- **Prefer calling the generic lemma directly** at the call site over writing a
  thin curve-only wrapper.
- **Avoid "connection" lemmas** whose precondition is exactly another lemma's
  postcondition — inline that proof at the single call site instead.

## 3. Nonlinear arithmetic: decompose, don't hammer

The single most important pattern, and the one
[diagnostics Pattern 3/6](diagnostics.md) is about. Nonlinear goals (the 5×5
limb-product expansions pervasive in field arithmetic) are **broken into
pre-proven 2-term identities**, not handed to one bulk tactic.

> In the sample: **`lemma_mul_is_*` ×95 vs `by (nonlinear_arith)` ×13 (~7:1)**;
> `broadcast use` ×0, `calc!` ×0.

The canonical example is `common_lemmas/mul_lemmas.rs`: a recursively-built
ladder `lemma_mul_distributive_{3..8}_terms` plus reorder helpers
(`lemma_mul_w0_and_reorder`, `lemma_mul_si_vi_and_reorder`,
`lemma_product_square_factorize`), each rung a wrapped 2-term
`lemma_mul_is_distributive_add` / `_associative` / `_commutative`. One docstring
states the intent outright: it *"avoids the 15-line manual associativity/
commutativity chain."* vstd `int` lemmas are bridged to `nat`/`u64` via thin
wrappers (`lemma_nat_distributive`, `lemma_m`).

**The actionable invariant** (codex's phrasing, adopted): *don't solve a 5×5
expansion with one bulk `nonlinear_arith` or `broadcast` hammer — decompose it
through `common_lemmas::mul_lemmas`.* `by (nonlinear_arith)` is canonical and
fine for **small** inequalities / transitivity chains; it's the bulk expansion
that blows the rlimit.

## 4. The rlimit discipline is *scoping*, not bumping

The proof-helper SKILL gives an explicit priority order:

> **scope lemma calls in `assert(...) by {}` > explicit triggers > bundled
> predicates > `opaque` + `reveal` > bump `rlimit` (last resort).**

The ~60% `by {}`-to-`assert` ratio (§1) is that rule made physical: each
lemma's facts stay quarantined in its own block instead of polluting the global
SMT context. `opaque`/`reveal` are an escape hatch, not a default —
`reveal` ×5, `opaque` ×0 in the sample. Treat `reveal` as **"use only at a
deliberate abstraction boundary"** (e.g. a spec-bridge invariant), not as
something to avoid and not as something to reach for routinely.

## 5. Tactic choice is goal-shaped

- **Bit/byte facts → `by (bit_vector)`** (×42): shifts, masks, `& 1` ↔ `% 2`,
  top-bit extraction.
- **Modular arithmetic → named vstd `div_mod` lemmas** (`lemma_small_mod`,
  `lemma_sub_mod_noop`, `lemma_mod_bound`), not raw solving.
- **Structural recursion → `decreases`** (×32): induction over byte index, limb
  count, or exponent `k`.
- **`by (compute)`**: fine for tiny *literal-constant* facts; **avoid over
  exec-derived values** (interpreter-stability footgun documented in
  `common-issues.md`).

## 6. Spec & axiom conventions (the soundness story)

### Two layers, split deliberately
- **Lemma proofs avoid `assume(...)` and `external_body`** entirely (×0 in the
  sample). A lemma that "needs" either is a red flag.
- **Exec/API wrappers** (the second layer, e.g. trusted constants, external
  crates, iterator adapters) legitimately use `#[verifier::external_body]` — but
  only when **named, documented with a `VERIFICATION NOTE`, and backed by a
  runtime test or a contract** (`ensures`). E.g. `docs/ristretto_finished.rs`
  carries explicit `external_body` wrappers in exactly this disciplined form.

### Visibility & naming
- **`open spec fn` is the default** (×39 vs `closed` ×2, `uninterp` ×0).
  `closed` is uncommon but **reserved for deliberate abstraction boundaries** —
  hardcoded data, a `choose` body, or accessor bodies exported through a tiny
  bridge lemma (the `edwards_x/y/z/t` + `lemma_unfold_edwards` pattern in the
  next section). `uninterp` only for external primitives, always paired with an
  admitted axiom.
- **Names carry role:** `spec_` (exec-correspondence target), bare (pure math),
  `is_` (validity predicate), `axiom_` (admitted), `lemma_` (fully proved).

### Two intentional patterns worth naming
- **Closed-spec + exporter-lemma.** A `closed spec fn` is paired with a tiny
  exporter `proof fn` that re-states one fact so other modules can use it
  without seeing the body. `specs/edwards_specs.rs` does this twice: the
  basepoint (`spec_ed25519_basepoint` + `lemma_ed25519_basepoint_y`) and the
  coordinate accessors (`edwards_x/y/z/t` closed, with `lemma_unfold_edwards`
  ensuring `edwards_x(p) == p.X`, …). The exporter's body is **empty** — it
  works because it sits in the *same spec module* as the closed definitions, so
  field projection is legal there. (This is the bridge a stripped-cut agent must
  *reconstruct in `specs/edwards_specs.rs`*, or *call* from a consumer — not
  reinvent as a new accessor.)
- **Honest, pinned axioms.** Hardcoded-data facts SMT can't discharge
  (`axiom_ed25519_basepoint_table_valid`, `axiom_four_torsion_affine`) keep an
  `admit()` body with a `VERIFICATION NOTE`, and are backed by a `#[cfg(test)]`
  runtime test of the constant's structure (`edwards_specs.rs` eight-torsion
  tests).

## 7. Comments are operational and paper-pinned

- **Spec modules open with a `## References` block** citing the actual papers /
  RFCs the math comes from ([RFC8032], [BBJLP2008] Twisted Edwards,
  [HWCD2008] extended coordinates).
- **Lemmas carry a `## Mathematical Proof` block** spelling out the case
  analysis in prose-math *before* the Verus encodes it (see
  `decompress_lemmas.rs`: "p is odd, x is even ⇒ p − x is odd ⇒ LSB = 1").
- **Inline comments use a strict `// var = formula` operational style**
  (`// e(l) = l0 + l1·2^51 + … in Z_p, for p = 2^255 − 19`) — the math meaning,
  never prose narration.
- The `/* ORIGINAL CODE: … */` and `// PROOF BYPASS` tags belong to the
  **spec-drafting phase** (exec refactors / deferred proofs); they're absent
  from finished lemma files, and their absence is itself a "this proof is done"
  signal.

## 8. The development loop

The prescribed workflow:

1. **Understand** the exact goal (`ensures`/`requires`); list every
   `admit()`/`assume(...)`.
2. **Reuse first** — search the lemma library (§2) before writing anything new;
   if you must add a lemma, write the **smallest specialized** one that unblocks
   you, generalize later.
3. **Develop by "moving `assume(false)`"** — sketch the structure with
   `assume(false)` at each step, then replace one at a time, re-verifying after
   each. This is a **temporary local scaffold only**: every `assume(false)` must
   be removed before the proof is considered complete or scoreable (it
   discharges any goal — a leftover one is a fake-green, and conflicts with §6's
   "lemma proofs avoid `assume`").
4. **Apply goal-shaped tactics** (§5).
5. **Verify incrementally:** `--verify-function` → `--verify-module` → whole
   crate; run `verusfmt` on touched files, then re-verify (formatting can
   perturb proof blocks).
6. **Clean up:** delete redundant asserts one at a time; prefer
   `assert(fact) by { lemma(); }` over a floating `lemma();`.

## TL;DR for an agent proving here

Search the lemma library first. Decompose nonlinear goals into 2-term
`lemma_mul_is_*` steps rather than one bulk `nonlinear_arith`/`broadcast`. Wrap
every lemma call in its own `assert(...) by {}` — that's both the readability
*and* the rlimit strategy. Use `bit_vector` for bits and named `div_mod` lemmas
for modular facts. Keep specs `open`, name by role, and leave only honest
paper-pinned `axiom_` + `admit` (plus a runtime test) for hardcoded data. In a
proof, never reach for `assume`/`external_body`; if an exec wrapper genuinely
needs `external_body`, name it, document it, and back it with a test or
contract.

---

*Caveat repeated: counts are sampled (56/87 lemma+spec files + skill notes), not
a full-tree census, and exclude the exec-method-proof layer. The zero-counts
(`broadcast use`, `calc!`, `opaque`, `assume`/`external_body` in lemmas) held as
the sample grew but should be read as "not canonical for these cases," not as
hard prohibitions.*
