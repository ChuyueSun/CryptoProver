## Decompose hard admits

If the function around an `admit()` is >100 lines OR its `ensures`
clause has ≥3 conjuncts, **do not write a single `proof { ... }`
block at the admit site**. The Z3 context at that point is too rich
and the solver will time out. Instead:

**(a) Conjunct-wise asserts in the body.** For each `ensures`
conjunct, find the point in the body where its premises become
available (typically right after the relevant intermediate value is
bound to a `let ghost`) and write:

```rust
assert(<conjunct>) by {
    lemma_call_1(...);
    lemma_call_2(...);
};
```

at THAT site. Each assert has a small local context that Z3 can
close in isolation. This is the dominant proof shape in this
codebase — grep `assert.*by` in any verified file to see it.

**(b) In-file or sibling-helper proof fns.** When a sub-proof
exceeds 10 lines, extract it into a `proof fn lemma_<purpose>(...)
requires ... ensures ...` and call it from the original site. The
lemma's `requires` makes Z3's job local. Place the helper in the
same target file when it's purely local; place it in a sibling
`lemmas/<area>_lemmas/*.rs` when it bridges multiple admit sites
or could be reused (see rule 4).

**(c) Iterate one conjunct at a time, DO NOT REVERT.** If the
function has 8 ensures conjuncts, land conjunct 1 (verify
`verus_check` passes), then attack conjunct 2. Partial
decomposition with some `admit()`s remaining is FINE between rounds
— rule 3 explicitly allows it. The task end-state must satisfy
`admits_remaining ≤ admits_at_start`, but intermediate rounds may
freely use `admit()` as a WIP marker.

### Worked example (sketch)

A function with `ensures result.X == spec_x(s), result.Y == spec_y(s)`
becomes:

```rust
// body computes intermediate ghost values ...
let ghost x_nat = fe51_as_canonical_nat(&result.X);
assert(x_nat == spec_x(fe51_as_canonical_nat(&s))) by {
    lemma_intermediate_step_1(...);
    lemma_intermediate_step_2(...);
};
// ... more body ...
let ghost y_nat = fe51_as_canonical_nat(&result.Y);
assert(y_nat == spec_y(fe51_as_canonical_nat(&s))) by {
    lemma_y_bridge(...);
};
```

If `lemma_intermediate_step_*` don't exist, define them yourself in
this file or in a sibling per rule 4.
