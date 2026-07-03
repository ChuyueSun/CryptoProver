# Stage-3 field-floor certificate — canonical run record (096/097)

The single citable record of the Class B claim run: packet, sterility
evidence, attempts, verification battery, marker, cost. Companion docs:
`stage3_proof_evolution.md` (how the proof grew),
`gt_divergence_097_certificate.md` (architecture vs GT),
`ladder_stage1_results.md` (Class A context + incident ledger + claim
boundary).

## The cut (init state)

Full generated `field_floor.json` manifest applied at `corefloor-base-103b92b9`
(proven tip: whole-crate 2066 verified / 0 errors): all **22 lemma homes
deleted** (235 lemmas: signature+contract+body) and all **4 API files
proof-stripped** (montgomery 16 / ristretto 21 / edwards 30 / scalar 33 =
100 inline proof blocks; headers/contracts/exec byte-identical). Editable
set = exactly those 26 files; everything else frozen (FROZEN_EDIT guard).
Baseline: preflight packet census **130 errors** (124 frozen-callsite
E0425 + 5 editable + 1 wrapper; `baseline_pin_census.json` in the VM1
preflight artifacts). First archived in-run whole-crate measurement:
**166 verification-class errors** (round 1, after initial compile
scaffolding). [An earlier "289-error baseline" figure was uncited and is
withdrawn.]

## Measured pins (what constrains the reconstruction besides the API contract)

- Frozen API `requires`/`ensures` + all `spec fn` definitions
  (`--check-spec-defs`) + trusted floor (vstd, field layer, exec code, 48
  `axiom_*`).
- **Pin census (pre-registered preflight gate)**: 124 frozen-callsite
  references to deleted lemma names (name/signature pins concentrated in
  backend consumer proofs); deleted homes retain filenames (skeleton
  visibility). Both declared in the packet before launch.
- Declared prompt priors (verbatim in `ladder_stage1_results.md`):
  "dozens per file" granularity phrase + "fine-grained chains" maxim —
  disclosed, kept at H0.
- **GT natural-language comments in kept files** (measured post-hoc, user
  question 2026-07-03): ≈5.8k comment lines survived the cut across the 26
  editables (block-aware count, dual-verified) — module docs
  in emptied homes, section banners (some with mathematical identities),
  exec-adjacent VERIFICATION NOTEs (incl. montgomery.rs's rlimit-fragility
  note). Stream analysis: consulted, not merely visible (assistant-role
  echoes of four channels; torsion docs unechoed). Applies to Class A
  identically; see
  `gt_divergence_097_certificate.md` caveat.

## Sterility (verified at both launches)

Fresh results root (no ladder failure_memory/proven_registry/claude_memory);
container mounts = own work+results only; `/opt/warm-src` absent;
in-container `run.py` sha == reviewed `a56f284` (`ea25d7df…`); rendered
prompt audited (lane-neutral, no ladder-derived content); base pristine
(every bank marker empty-diff — verified across all 40+ refs); no GT tree
readable anywhere in the environment; model `claude-fable-5`.

## Attempts (pre-registered multi-attempt protocol, H0 hint level throughout)

| attempt | run id | start (Z) | wall | rounds | end | whole-crate |
|---|---|---|---|---|---|---|
| 1 | field_floor_stage3_096_480_fable | 07-02 18:04 | 8.1 h | 4 | LIMIT (clean budget exhaustion; zero gate events) | 166→154→113→**69** |
| 2 | …_097_480_fable (seeded: `--seed-wip` attempt-1 patch, launcher guard = 21 editable files; + own failure memory) | 07-03 03:14 | 3.3 h | 1 | **COMPLETE** | 69→**0** |

Total agent time 11.4 h; recorded API cost **$466.99** (round-level
`claude_usage`; attempt-1's deadline-killed round 4 unrecorded, so true
total slightly higher). Seed continuity exact (attempt-2's first
whole-crate measurement = 69). No hint escalation was ever used: the
pre-registered H1–H4 ladder went untouched.

## Verification battery (all green; dual-executed claude-relaunch × codex)

1. Harness seal: COMPLETE; final_error_count 0, hard admits 0, spec drift
   0, intentional axiom admits 5 (preserved trusted-floor items).
2. Independent fresh-container ro-mount whole-crate, default rlimit, VM1:
   **okay=true, 0 errors, 2031 verified**.
3. Second-machine untouched-copy whole-crate, VM2: **identical verdict**
   (0 errors / 2031 verified).
4. Crate-wide span-aware inventory: non-axiom admits 0; axiom count
   **exactly 48**; untracked files 0; `external_body` byte-stable
   (0/5/6/5).
5. Exec-immutability (mechanical erasure audit, user policy): zero base
   exec lines removed across both attempts in all 4 API files; all
   additions ghost. Deletion-side audits clean both attempts.
6. Scope: every modified file within the editable 26 (launcher seed-guard
   + git audits concur); no PROBE artifacts; forbidden-construct deltas 0.
7. rlimit attrs (stratified ruling): 2×20 within GT envelope; 3×150 +
   4×300 beyond it; targeted single-attr-removal gates show **exactly one
   load-bearing**: `scalar::non_adjacent_form` (150). The 600/900
   experiments did not survive to seal.
8. Certificate marker: `corefloor-plus-fieldfloor-certificate-097`
   (+alias) @ `06160297ecb4`, parent 095's marker, tree `177914d41ffc` ==
   base; empty diff verified by both agents.

## Result statement (paper form)

Given only the user-facing API contracts, frozen specifications, a trusted
arithmetic floor, and the measured pin set (124 frozen call sites +
file skeleton + declared priors), a claude-fable-5 agent under the a56f284
harness reconstructed the entire deleted proof cone of the dalek-lite
field-floor cut — +11,024/−22,753 lines by git numstat over the 26
editable files (48.5% of GT's proof mass), 196 lemmas vs GT's 235 (83%) —
to whole-crate 0 errors at
default SMT limits, in 11.4 agent-hours across two pre-registered attempts,
with zero integrity violations, verified independently on two machines.
One function retains a beyond-GT solver budget (`non_adjacent_form`,
rlimit 150); all other budget attrs are droppable.

Evidence archives: VM1 `stage3_evidence_backups/{096_20260702T235159Z,
096_final_031656Z, 097_certificate_064133Z}` (full raw streams both
attempts, tree patches, verify JSONs, seed patch); ledger `AGENT_DEBATE.md`
(joint audit trail); local mirrors `stage3_local_evidence/`.
