#!/usr/bin/env bash
# demo_decompress.sh — one-command backend for the decompression website demo.
#
# Owns the ENTIRE machine-specific setup (python/verus/toolchain/auth PATH
# prelude + input prep + detached launch) so a website can shell out to a
# single command and then just poll results/<run-id>/edwards/ on disk.
#
# Difficulty rungs — the "how much is stripped" toggle (pick one):
#   --formal-spec  → run.py --experiment-mode proof-only  (gate ON)
#                    prep: admit proof bodies, KEEP contracts + docs
#                    → agent rebuilds PROOFS only
#   --no-spec      → run.py --experiment-mode spec-proof  (gate OFF)
#                    prep: strip fn-header contracts + admit bodies (docs kept)
#                    → agent rebuilds CONTRACT + PROOFS
#   --no-lemmas    → run.py --experiment-mode spec-proof  (gate OFF)
#                    prep: DELETE every proof lemma (doc + signature + body)
#                    → agent INVENTS the helper lemmas from the edwards.rs
#                      callsites
#   --no-anchor-proof → run.py --experiment-mode contract-only  (gate ON)
#                    prep: strip decompress's OWN proof body too (its inline
#                    `proof { … }` blocks) AND delete the decompress helper
#                    lemmas that nothing else still references. The anchor's
#                    CONTRACT (signature + requires/ensures) stays byte-
#                    identical to main and is gate-frozen.
#                    → agent reconstructs decompress's orchestration proof AND
#                      invents the helper lemmas from scratch (hardest rung).
#                      First rung where the ANCHOR file is editable; soundness
#                      comes from the frozen contract, not a fixed proof body.
#   --no-bridge-specs → run.py --experiment-mode bridge-specs  (gate ON)
#                    prep: DELETE the two shared Montgomery↔Edwards bridge
#                    `open spec fn`s (montgomery_to_edwards_affine,
#                    edwards_y_from_montgomery_u) from the editable bridge
#                    module specs/decompress_bridge_specs.rs. decompress's
#                    lemmas + proof AND montgomery::to_edwards stay frozen.
#                    → agent reconstructs the birational MAP DEFINITIONS so the
#                      WHOLE crate verifies (hardest rung). Soundness: the
#                      frozen consumers (to_edwards's proof, decompress's
#                      contract) PIN the map — a wrong def fails them. run.py
#                      adds whole-crate verify + a frozen-file guard (only the
#                      bridge module may change; any other edit ⇒ FROZEN_EDIT).
#   --no-bridge-lemmas → run.py --experiment-mode bridge-full  (gate ON)
#                    PURE PROOF RECONSTRUCTION — every spec definition is frozen
#                    (incl. the Montgomery↔Edwards map); the agent rebuilds only
#                    proofs. prep: DELETE every decompress-path lemma outright
#                    (signature + contract + body) — 5 in decompress_lemmas.rs,
#                    5 in curve_equation_lemmas.rs. The frozen proofs that call
#                    them stop compiling until the agent re-derives each lemma
#                    (signature + contract + proof). The map module
#                    decompress_bridge_specs.rs stays at clean main and is FROZEN.
#                    Editable: decompress_lemmas.rs, curve_equation_lemmas.rs.
#                    → agent re-derives all 10 deleted lemmas + any helpers it
#                      needs (incl. the curve facts x_zero_implies_y_squared_one
#                      and unique_x_with_parity), so the WHOLE crate verifies
#                      (hardest rung). Contract integrity is STRUCTURAL: the
#                      user-facing API contracts and every spec they are written
#                      in are frozen, so no re-derived lemma contract can weaken
#                      them — a too-weak one merely fails the frozen proof (not
#                      COMPLETE), never a silent weakening. The ~47 unrelated
#                      group-law lemmas are left intact (gate freezes contracts).
#
#   --no-ristretto-proof → run.py --experiment-mode bridge-full  (gate ON)
#                    ONE LAYER UP from the edwards rungs. The anchor under test is
#                    RistrettoPoint::decompress (ristretto.rs) — a user-facing API
#                    built ON TOP of Edwards. PURE PROOF RECONSTRUCTION with the
#                    ENTIRE edwards/montgomery/field/number-theory substrate FROZEN
#                    underneath, plus the ristretto spec vocabulary
#                    (specs/ristretto_specs.rs) and the ristretto lemma layer
#                    (lemmas/ristretto_lemmas/*, incl. axioms.rs) all FROZEN.
#                    prep: strip the proof bodies of decompress + its two dedicated
#                    proof helpers step_1 / step_2 (the `mod decompress` step fns,
#                    used ONLY by decompress — the moral analog of edwards's
#                    decompress_lemmas) via --strip-proof-fn, keeping every
#                    signature + requires/ensures + executable code byte-identical
#                    to main. NO --delete-fn: decompress's proof tree calls ZERO
#                    deletable ristretto lemmas (it leans entirely on the frozen
#                    field/edwards substrate + the two frozen ristretto axioms
#                    axiom_ristretto_decode_on_curve / _in_even_subgroup), so there
#                    is no decompress-only ristretto lemma layer to remove.
#                    Editable: ristretto.rs ONLY.
#                    → agent reconstructs the entire ristretto decompress proof
#                      layer (orchestration + both step proofs) so the WHOLE crate
#                      verifies. Contract integrity is STRUCTURAL for the anchor:
#                      decompress/step_1/step_2 contracts reference ONLY frozen spec
#                      fns (spec_ristretto_decompress, ristretto_decode_*,
#                      is_well_formed_edwards_point, is_in_even_subgroup, … — all in
#                      frozen files), and the spec gate snapshots every allow-edit
#                      file's headers, so no edit can weaken the anchor's meaning.
#                      (ristretto.rs DOES carry its own unrelated open spec fns —
#                      batch_state_*, from_spec, eq_spec, neg_spec — for the
#                      compress/From/Eq/Neg APIs; the decompress contract references
#                      none of them, and they are pinned by their own FROZEN
#                      consumers + the whole-crate verify. run.py adds whole-crate
#                      verify + a frozen-file guard, so any edit outside
#                      ristretto.rs ⇒ FROZEN_EDIT.)
#
#   --no-fullstack-proof → run.py --experiment-mode bridge-full  (gate ON)
#                    THE WHOLE DECOMPRESS PROOF STACK, all three layers at once:
#                    no-api-proof (edwards.rs::decompress + montgomery.rs::to_edwards
#                    proof bodies stripped; all 10 decompress-path lemmas deleted
#                    across decompress_lemmas.rs + curve_equation_lemmas.rs) PLUS
#                    the ristretto rung (ristretto.rs decompress + step_1 + step_2
#                    proofs stripped). Five editable files; the largest
#                    reconstruction of any rung. Frozen: the Montgomery↔Edwards map
#                    (decompress_bridge_specs.rs), every specs/* vocabulary,
#                    ristretto_lemmas/* (incl. axioms), and the whole field/
#                    number-theory substrate. Every surviving API contract is
#                    gate-frozen (run.py snapshots all five allow-edit files).
#                    → agent reconstructs the ENTIRE decompress proof tree — three
#                      API orchestration proofs + the two ristretto step proofs +
#                      all 10 deleted lemmas — from the five frozen contracts +
#                      frozen specs alone, so the WHOLE crate verifies. Contract
#                      integrity is per-anchor structural: each API ensures is
#                      frozen and written only in frozen spec fns, so no re-derived
#                      lemma/proof can weaken it. Editable files DO carry their own
#                      unrelated open spec fns (edwards.rs 17, montgomery.rs 2,
#                      ristretto.rs 7 — Eq/Add/Sub/From/Neg/well_formed etc.); none
#                      is referenced by a decompress-path contract, and each is
#                      pinned by its own frozen consumer + whole-crate verify
#                      (audited byte-identical post-run). Hardest rung built.
#
#   --strip-docs   modifier (only with --no-spec): also remove the `///` doc
#                  comments, which carry the informal proof sketch — so only
#                  the bare signature survives. Default: docs kept.
#
# The demo edits decompress_lemmas.rs (all rungs); --no-anchor-proof ALSO lets
# the agent edit the anchor's proof body (its contract stays frozen).
# The anchor (fixed contract under test) is edwards.rs::decompress.
#
# Contract with the caller:
#   - detaches the run (survives the caller's process-group teardown) and
#     RETURNS immediately,
#   - prints two machine-readable lines to stdout:
#         RUN_ID <id>
#         RESULTS <abs path to results/<id>/edwards>
#   - exits 0 once the run is LAUNCHED; exits nonzero ONLY on launch/setup
#     failure. The PROOF outcome is read by the caller from result.json.
#
# Usage (pick exactly one rung):
#   ./demo_decompress.sh --formal-spec            --run-id web_1 [--rounds 4] [--budget 45] [--model opus]
#   ./demo_decompress.sh --no-spec                --run-id web_2
#   ./demo_decompress.sh --no-spec --strip-docs   --run-id web_3
#   ./demo_decompress.sh --no-lemmas              --run-id web_4
#   ./demo_decompress.sh --no-anchor-proof        --run-id web_5 [--rounds 6] [--budget 90]
#   ./demo_decompress.sh --no-bridge-specs        --run-id web_6 [--rounds 7] [--budget 120]
#   ./demo_decompress.sh --no-bridge-lemmas       --run-id web_7 [--rounds 10] [--budget 180]
#   ./demo_decompress.sh --no-api-proof           --run-id web_8 [--rounds 12] [--budget 240]
#   ./demo_decompress.sh --no-ristretto-proof     --run-id web_9 [--rounds 12] [--budget 240]
#   ./demo_decompress.sh --no-fullstack-proof     --run-id web_10 [--rounds 16] [--budget 240]
#
# Env overrides (all have verified defaults for this machine):
#   DALEK_UV_PY_BIN, DALEK_VERUS_DIR, DALEK_PROJECT, DALEK_GITROOT, DALEK_VSTD
#   CLAUDE_CODE_OAUTH_TOKEN   (or DALEK_DEMO_TOKEN_FILE → file holding it;
#                              if neither set, falls back to the keychain login)
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── machine-specific defaults (override via env) ─────────────────────────────
UV_PY_BIN="${DALEK_UV_PY_BIN:-/path/to/python3/bin}"
VERUS_DIR="${DALEK_VERUS_DIR:-/tmp/verus-rel/verus-arm64-macos}"
PROJECT="${DALEK_PROJECT:-/private/tmp/dalek-spec-strip/curve25519-dalek}"
GITROOT="${DALEK_GITROOT:-/private/tmp/dalek-spec-strip}"
VSTD="${DALEK_VSTD:-/path/to/verus/vstd}"

# DEP_SUB is relative to the cargo member dir ($PROJECT); git checkout needs it
# relative to the workspace gitroot ($GITROOT) — computed after preflight.
DEP_SUB="src/lemmas/edwards_lemmas/decompress_lemmas.rs"
ANCHOR="$PROJECT/src/edwards.rs"
DEP="$PROJECT/$DEP_SUB"
# Editable bridge module for the --no-bridge-specs rung (Montgomery↔Edwards map).
BRIDGE_SUB="src/specs/decompress_bridge_specs.rs"
BRIDGE="$PROJECT/$BRIDGE_SUB"
# Edwards group-law library — editable ONLY for --no-bridge-lemmas, which strips
# the few curve lemmas on the decompress path (the other ~47 group-law lemmas
# stay in place; the spec gate freezes their contracts).
CURVE_SUB="src/lemmas/edwards_lemmas/curve_equation_lemmas.rs"
CURVE="$PROJECT/$CURVE_SUB"
# montgomery.rs — editable ONLY for --no-api-proof, which strips to_edwards's own
# proof body (its contract stays frozen via the spec gate, which run.py snapshots
# for every allow-edit file).
MONT="$PROJECT/src/montgomery.rs"
# ristretto.rs — editable ONLY for --no-ristretto-proof, which strips the
# RistrettoPoint::decompress API proof layer (decompress + step_1 + step_2 proof
# bodies). Their contracts stay frozen via the spec gate (run.py snapshots every
# allow-edit file). One layer ABOVE edwards: the whole edwards/field substrate +
# specs/ristretto_specs.rs + lemmas/ristretto_lemmas/* (incl. axioms) stay frozen.
RISTRETTO="$PROJECT/src/ristretto.rs"

# ── args ─────────────────────────────────────────────────────────────────────
MODE=""           # formal-spec | no-spec | no-lemmas | no-anchor-proof |
                  # no-bridge-specs | no-bridge-lemmas
STRIP_DOCS=0
RUN_ID=""
ROUNDS=4
BUDGET=45
ROUNDS_SET=0      # track explicit override so per-rung defaults can apply
BUDGET_SET=0
MODEL="opus"

die() { echo "demo_decompress: $*" >&2; exit 2; }
usage() { sed -n '2,80p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --formal-spec)     MODE="formal-spec";     shift ;;
    --no-spec)         MODE="no-spec";         shift ;;
    --no-lemmas)       MODE="no-lemmas";       shift ;;
    --no-anchor-proof) MODE="no-anchor-proof"; shift ;;
    --no-bridge-specs) MODE="no-bridge-specs"; shift ;;
    --no-bridge-lemmas) MODE="no-bridge-lemmas"; shift ;;
    --no-api-proof)     MODE="no-api-proof";     shift ;;
    --no-ristretto-proof) MODE="no-ristretto-proof"; shift ;;
    --no-fullstack-proof) MODE="no-fullstack-proof"; shift ;;
    --strip-docs)      STRIP_DOCS=1;           shift ;;
    --run-id)          RUN_ID="$2";            shift 2 ;;
    --rounds)          ROUNDS="$2"; ROUNDS_SET=1; shift 2 ;;
    --budget)          BUDGET="$2"; BUDGET_SET=1; shift 2 ;;
    --model)           MODEL="$2";             shift 2 ;;
    -h|--help)         usage 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

[ -n "$MODE" ]   || die "mode required: --formal-spec | --no-spec | --no-lemmas | --no-anchor-proof | --no-bridge-specs | --no-bridge-lemmas | --no-api-proof | --no-ristretto-proof | --no-fullstack-proof"
[ -n "$RUN_ID" ] || die "--run-id required"
if [ "$STRIP_DOCS" = "1" ] && [ "$MODE" != "no-spec" ]; then
  die "--strip-docs only applies to --no-spec"
fi
# Hardest rung leans on step_1/step_2 contracts + invented lemmas → lower
# convergence; give it more headroom unless the caller overrode it.
if [ "$MODE" = "no-anchor-proof" ]; then
  [ "$ROUNDS_SET" = "1" ] || ROUNDS=6
  [ "$BUDGET_SET" = "1" ] || BUDGET=90
fi
# no-bridge-specs reconstructs the Montgomery↔Edwards map from scratch and is
# gated on a WHOLE-CRATE verify each round (the frozen consumers that pin it),
# so it needs the most headroom of any rung.
if [ "$MODE" = "no-bridge-specs" ]; then
  [ "$ROUNDS_SET" = "1" ] || ROUNDS=7
  [ "$BUDGET_SET" = "1" ] || BUDGET=120
fi
# no-bridge-lemmas strips the bridge specs, the decompress lemma chain, AND the
# decompress-path curve lemmas, so it reconstructs strictly more than every other
# rung (map defs + entry/curve proofs + reinvented helpers across three files).
# Most headroom of all.
if [ "$MODE" = "no-bridge-lemmas" ]; then
  [ "$ROUNDS_SET" = "1" ] || ROUNDS=10
  [ "$BUDGET_SET" = "1" ] || BUDGET=180
fi
# no-api-proof = no-bridge-lemmas PLUS the two public-API proof bodies stripped
# (decompress + to_edwards). The agent rebuilds the ENTIRE proof layer from the
# two frozen API contracts + frozen specs. The most headroom of all.
if [ "$MODE" = "no-api-proof" ]; then
  [ "$ROUNDS_SET" = "1" ] || ROUNDS=12
  [ "$BUDGET_SET" = "1" ] || BUDGET=240
fi
# no-ristretto-proof = pure proof reconstruction ONE LAYER UP (ristretto), with
# the entire edwards/field substrate frozen underneath. The frozen substrate the
# agent must read to discharge step_2's invsqrt/decode obligations is large, so
# the read/context cost is high (expect the bloat-reset to matter). Most headroom,
# kept under the 5-hour session window (budget is wall-clock minutes).
if [ "$MODE" = "no-ristretto-proof" ]; then
  [ "$ROUNDS_SET" = "1" ] || ROUNDS=12
  [ "$BUDGET_SET" = "1" ] || BUDGET=240
fi
# no-fullstack-proof = the WHOLE decompress proof stack across all THREE layers
# (ristretto + edwards + montgomery) stripped at once: no-api-proof's edwards/
# montgomery API proofs + 10 decompress-path lemmas PLUS the ristretto decompress
# proof layer. Five editable files, the largest reconstruction of any rung. Most
# headroom of all, kept under the 5-hour session window.
if [ "$MODE" = "no-fullstack-proof" ]; then
  [ "$ROUNDS_SET" = "1" ] || ROUNDS=16
  [ "$BUDGET_SET" = "1" ] || BUDGET=240
fi

# The anchor under test (run.py target). edwards.rs for the edwards rungs;
# ristretto.rs (the topmost anchor) for --no-ristretto-proof and the full-stack
# rung --no-fullstack-proof (which also makes edwards.rs/montgomery.rs editable).
TARGET="$ANCHOR"
case "$MODE" in no-ristretto-proof|no-fullstack-proof) TARGET="$RISTRETTO" ;; esac

# ── env prelude (the part the website must NOT have to re-derive) ─────────────
export PATH="$UV_PY_BIN:$VERUS_DIR:$PATH"

# auth: prefer an explicit token (env or file); else fall back to keychain login.
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -n "${DALEK_DEMO_TOKEN_FILE:-}" ] && [ -f "$DALEK_DEMO_TOKEN_FILE" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat "$DALEK_DEMO_TOKEN_FILE")"
  export CLAUDE_CODE_OAUTH_TOKEN
fi

# ── preflight (fail fast & loud BEFORE launching) ────────────────────────────
command -v python3   >/dev/null || die "python3 not on PATH ($UV_PY_BIN missing?)"
case "$(python3 -c 'import sys;print(sys.version_info[:2]>=(3,10))' 2>/dev/null)" in
  True) : ;; *) die "python3 is too old (need 3.10+); got $(python3 --version 2>&1)";;
esac
command -v cargo-verus >/dev/null || die "cargo-verus not on PATH ($VERUS_DIR missing?)"
command -v claude    >/dev/null || die "claude not on PATH"
[ -d "$PROJECT" ]    || die "project worktree missing: $PROJECT"
[ -f "$ANCHOR" ]     || die "anchor missing: $ANCHOR"
[ -f "$DEP" ]        || die "dep missing: $DEP"
[ -f "$TARGET" ]     || die "target missing: $TARGET"

# Per-slot results root (isolates the cumulative catalog/failure/registry JSON
# across pool slots and from local runs). Default: the harness-local results/.
RESULTS_ROOT="${DALEK_RESULTS:-$HARNESS_DIR/results}"
mkdir -p "$RESULTS_ROOT"

# ── safety interlock: at most one in-flight run per worktree ──────────────────
# The website owns the K-slot semaphore; this is defense in depth so a pool bug
# can't silently clobber a tree mid-proof. Atomic acquire via mkdir; a crashed
# holder (dead pid) is reclaimed. NOT auto-released — the next caller reclaims
# once the recorded run.py pid is gone.
LOCK="$PROJECT/.demo_lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  held="$(cat "$LOCK/pid" 2>/dev/null || echo)"
  if [ -n "$held" ] && kill -0 "$held" 2>/dev/null; then
    die "worktree busy: $PROJECT held by pid $held (run $(cat "$LOCK/run_id" 2>/dev/null))"
  fi
  rm -rf "$LOCK"; mkdir "$LOCK" || die "could not acquire worktree lock: $LOCK"
fi
echo "$$" > "$LOCK/pid"; echo "$RUN_ID" > "$LOCK/run_id"

# ── one-time vstd/build warm (cold module-scoped check spuriously fails) ──────
WARM_SENTINEL="$PROJECT/target/.demo_warmed"
if [ ! -f "$WARM_SENTINEL" ]; then
  echo "demo_decompress: warming verus build (one-time, ~40s)…" >&2
  # warm on the committed (proven) state so it passes; ignore the verdict,
  # we only need vstd + deps compiled into the build cache.
  ( cd "$PROJECT" && cargo verus verify -p curve25519-dalek >/dev/null 2>&1 ) || true
  mkdir -p "$(dirname "$WARM_SENTINEL")"; : > "$WARM_SENTINEL"
fi

# ── prep the input: canonical + idempotent (reset → admit bodies → maybe strip)
# reset the dep to clean proven source first (the passes reset any prior proof)
DEP_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$DEP" "$GITROOT")"
git -C "$GITROOT" checkout main -- "$DEP_GIT_REL"
# Reset the ANCHOR to clean main in EVERY mode too. Only --no-anchor-proof
# edits edwards.rs, but a prior no-anchor run leaves it dirty (a valid but
# non-pristine reconstruction). Without this, that stale anchor persists into a
# later --no-spec/--no-lemmas/--formal-spec run on the same worktree — anchor
# cross-contamination. Resetting here keeps the anchor pristine for all rungs
# and the prep fully idempotent regardless of what ran before.
ANCHOR_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$ANCHOR" "$GITROOT")"
git -C "$GITROOT" checkout main -- "$ANCHOR_GIT_REL"

case "$MODE" in
  formal-spec)
    # proof-only: admit proof-fn bodies, keep contracts + docs
    python3 "$HARNESS_DIR/admit.py" "$DEP" --in-place --mode fn-bodies >/dev/null
    ;;
  no-spec)
    # spec-proof: strip fn-header contracts (+ docs if asked), then admit bodies
    STRIP_ARGS=( "$DEP" --in-place )
    [ "$STRIP_DOCS" = "1" ] && STRIP_ARGS+=( --strip-docs )
    python3 "$HARNESS_DIR/strip_specs.py" "${STRIP_ARGS[@]}" >/dev/null
    python3 "$HARNESS_DIR/admit.py" "$DEP" --in-place --mode fn-bodies >/dev/null
    ;;
  no-lemmas)
    # spec-proof (hardest): DELETE every proof lemma — doc + signature + body.
    # The agent must invent the helpers from the edwards.rs callsites. Names
    # are discovered from the clean source so we don't hardcode them.
    DEL_ARGS=( "$DEP" --in-place )
    while IFS= read -r name; do DEL_ARGS+=( --delete-fn "$name" ); done < <(
      grep -oE 'proof fn [a-zA-Z0-9_]+' "$DEP" | awk '{print $3}'
    )
    python3 "$HARNESS_DIR/strip_specs.py" "${DEL_ARGS[@]}" >/dev/null
    ;;
  no-anchor-proof)
    # contract-only (hardest): also strip decompress's OWN proof body in the
    # anchor and delete the decompress-only helper lemmas, while KEEPING the
    # anchor's signature + requires/ensures byte-identical to main. The agent
    # rebuilds decompress's orchestration proof AND invents the lemmas.
    # (The anchor was already reset to clean main in the generic prep above.)
    # 1) strip decompress's proof body (proof{} blocks + proof-only asserts),
    #    keeping signature + requires/ensures/decreases + the executable body.
    python3 "$HARNESS_DIR/strip_specs.py" "$ANCHOR" --in-place \
        --strip-proof-fn decompress >/dev/null
    # 2) delete ONLY the helper lemmas now unreachable — i.e. reachable solely
    #    from decompress's (just-stripped) proof. Deterministic explicit set
    #    (a fixed property of the pinned decompress source): the lemmas whose
    #    only caller was decompress's proof body. lemma_to_edwards_correctness
    #    (← montgomery.rs) and its callee lemma_decompress_spec_matches_point are
    #    KEPT — still referenced externally, so the crate keeps compiling and
    #    `cargo verus` fails on decompress's "postcondition not satisfied", NOT
    #    "cannot find function".
    python3 "$HARNESS_DIR/strip_specs.py" "$DEP" --in-place \
        --delete-fn lemma_decompress_valid_branch \
        --delete-fn lemma_decompress_field_element_sign_bit \
        --delete-fn lemma_sign_bit_after_conditional_negate >/dev/null
    ;;
  no-bridge-specs)
    # bridge-specs (hardest): DELETE the two shared Montgomery↔Edwards bridge
    # `open spec fn`s from the relocated, editable bridge module. The agent
    # reconstructs them so the WHOLE crate verifies. DEP (decompress lemmas)
    # and ANCHOR (edwards.rs) stay clean main — frozen, and they (plus
    # montgomery::to_edwards) PIN the reconstruction. The crate fails to
    # compile ("cannot find function …") until both specs are rebuilt.
    BRIDGE_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$BRIDGE" "$GITROOT")"
    git -C "$GITROOT" checkout main -- "$BRIDGE_GIT_REL"
    python3 "$HARNESS_DIR/strip_specs.py" "$BRIDGE" --in-place \
        --delete-fn montgomery_to_edwards_affine \
        --delete-fn edwards_y_from_montgomery_u >/dev/null
    ;;
  no-bridge-lemmas)
    # bridge-full (hardest): reconstruct the ENTIRE decompress proof tree (the
    # decompress lemmas + the decompress-path curve lemmas) with EVERY spec
    # definition FROZEN — including the Montgomery↔Edwards map. The agent rebuilds
    # only PROOFS; it never reconstructs a definition. This is the contract-
    # integrity choice: nothing the user-facing API contract is written in can be
    # touched, so the agent structurally cannot weaken the contract's meaning.
    # The split is a fixed property of the pinned source, named explicitly.
    #
    # The map (montgomery_to_edwards_affine, edwards_y_from_montgomery_u) is NOT
    # stripped — but we RESET decompress_bridge_specs.rs to clean main so the
    # frozen map is gt's definition (a prior run may have left a reconstructed
    # one in the worktree; since the file is frozen here, a leftover edit would
    # also trip FROZEN_EDIT on round 1).
    BRIDGE_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$BRIDGE" "$GITROOT")"
    git -C "$GITROOT" checkout main -- "$BRIDGE_GIT_REL"
    #
    #   Every lemma on the decompress path is DELETED outright (signature +
    #   contract + body) — the agent re-derives each one (signature forced by the
    #   frozen callsites; contract strong enough for every frozen caller; proof).
    #   Deleting them is contract-safe: the user-facing API ensures (decompress,
    #   to_edwards) live in frozen files and reference ONLY frozen spec fns (the
    #   editable lemma files contain zero spec fns), so no re-derived lemma
    #   contract can weaken them — a too-weak one merely fails the frozen proof
    #   (→ not COMPLETE), never a silent weakening.
    #
    #   decompress_lemmas.rs — delete all 5 (the crate stops compiling at the
    #   edwards.rs / montgomery.rs callsites until the agent rebuilds them):
    #     lemma_decompress_valid_branch       ← edwards.rs::decompress
    #     lemma_to_edwards_correctness        ← montgomery.rs::to_edwards
    #     lemma_decompress_field_element_sign_bit
    #     lemma_decompress_spec_matches_point
    #     lemma_sign_bit_after_conditional_negate
    python3 "$HARNESS_DIR/strip_specs.py" "$DEP" --in-place \
        --delete-fn lemma_decompress_valid_branch \
        --delete-fn lemma_to_edwards_correctness \
        --delete-fn lemma_decompress_field_element_sign_bit \
        --delete-fn lemma_decompress_spec_matches_point \
        --delete-fn lemma_sign_bit_after_conditional_negate >/dev/null
    #   curve_equation_lemmas.rs — delete the 5 decompress-path curve lemmas
    #   (frozen callers: decompress's step_2, to_edwards, niels, identity_on_curve).
    #   The other ~47 group-law lemmas are left intact; the spec gate snapshots
    #   curve_equation_lemmas.rs after the strip, so their contracts stay frozen.
    #     lemma_negation_preserves_curve
    #     lemma_affine_to_extended_valid
    #     lemma_edwards_affine_when_z_is_one
    #     lemma_x_zero_implies_y_squared_one
    #     lemma_unique_x_with_parity
    CURVE_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$CURVE" "$GITROOT")"
    git -C "$GITROOT" checkout main -- "$CURVE_GIT_REL"
    python3 "$HARNESS_DIR/strip_specs.py" "$CURVE" --in-place \
        --delete-fn lemma_negation_preserves_curve \
        --delete-fn lemma_affine_to_extended_valid \
        --delete-fn lemma_edwards_affine_when_z_is_one \
        --delete-fn lemma_x_zero_implies_y_squared_one \
        --delete-fn lemma_unique_x_with_parity >/dev/null
    ;;
  no-api-proof)
    # Everything no-bridge-lemmas does (map + all spec defs frozen; the 10
    # decompress-path lemmas deleted) PLUS strip the two PUBLIC-API proof bodies
    # themselves: decompress (edwards.rs) and to_edwards (montgomery.rs). Only
    # their CONTRACTS (signature + requires/ensures) stay — frozen, the anchor.
    # The agent rebuilds the ENTIRE proof layer (both API orchestration proofs +
    # all 10 lemmas) from the two frozen API contracts + frozen specs alone.
    #
    # The two API files become editable, so the frozen-file guard no longer
    # protects their contracts — run.py snapshots every allow-edit file, so the
    # spec gate freezes decompress's AND to_edwards's contracts (any header /
    # requires / ensures edit ⇒ SPEC_DRIFT). The map module + vocabulary stay
    # frozen by the file guard.
    #
    # 0) reset the two not-covered-by-generic-prep files to clean main: MONT
    #    (editable, about to be stripped) and BRIDGE (frozen map — a leftover
    #    edit would trip FROZEN_EDIT round 1). ANCHOR + DEP were reset above.
    for relsrc in "$MONT" "$BRIDGE"; do
      rel="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$relsrc" "$GITROOT")"
      git -C "$GITROOT" checkout main -- "$rel"
    done
    # 1) strip the two API proof bodies (inline proof{} blocks + asserts),
    #    keeping signature + requires/ensures + executable code.
    python3 "$HARNESS_DIR/strip_specs.py" "$ANCHOR" --in-place \
        --strip-proof-fn decompress >/dev/null
    python3 "$HARNESS_DIR/strip_specs.py" "$MONT" --in-place \
        --strip-proof-fn to_edwards >/dev/null
    # 2) delete all 10 decompress-path lemmas (same as no-bridge-lemmas). Reset
    #    CURVE first; DEP + ANCHOR were reset in the generic prep, MONT below.
    python3 "$HARNESS_DIR/strip_specs.py" "$DEP" --in-place \
        --delete-fn lemma_decompress_valid_branch \
        --delete-fn lemma_to_edwards_correctness \
        --delete-fn lemma_decompress_field_element_sign_bit \
        --delete-fn lemma_decompress_spec_matches_point \
        --delete-fn lemma_sign_bit_after_conditional_negate >/dev/null
    CURVE_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$CURVE" "$GITROOT")"
    git -C "$GITROOT" checkout main -- "$CURVE_GIT_REL"
    python3 "$HARNESS_DIR/strip_specs.py" "$CURVE" --in-place \
        --delete-fn lemma_negation_preserves_curve \
        --delete-fn lemma_affine_to_extended_valid \
        --delete-fn lemma_edwards_affine_when_z_is_one \
        --delete-fn lemma_x_zero_implies_y_squared_one \
        --delete-fn lemma_unique_x_with_parity >/dev/null
    ;;
  no-ristretto-proof)
    # ONE LAYER UP (ristretto). Strip the proof bodies of RistrettoPoint::
    # decompress AND its two dedicated proof helpers step_1 / step_2 (the
    # `mod decompress` step fns, used ONLY by decompress), keeping every
    # signature + requires/ensures + executable code byte-identical to main.
    # The agent reconstructs the entire ristretto decompress proof layer.
    #
    # NO --delete-fn: decompress's proof tree calls ZERO deletable ristretto
    # lemmas — it leans entirely on the FROZEN field/edwards substrate plus the
    # two FROZEN ristretto axioms (axiom_ristretto_decode_on_curve /
    # _in_even_subgroup in lemmas/ristretto_lemmas/axioms.rs). So there is no
    # decompress-only ristretto lemma layer to remove; the ristretto_lemmas/*
    # files (compress/coset/elligator) stay frozen by the file guard.
    #
    # Reset ristretto.rs to clean main first (a prior run may have left it
    # dirty; the strip resets any prior proof anyway). edwards.rs (ANCHOR) and
    # decompress_lemmas.rs (DEP) were reset to clean main in the generic prep
    # above — harmless here (both stay frozen for this rung).
    RIST_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$RISTRETTO" "$GITROOT")"
    git -C "$GITROOT" checkout main -- "$RIST_GIT_REL"
    python3 "$HARNESS_DIR/strip_specs.py" "$RISTRETTO" --in-place \
        --strip-proof-fn decompress \
        --strip-proof-fn step_1 \
        --strip-proof-fn step_2 >/dev/null
    ;;
  no-fullstack-proof)
    # The WHOLE decompress proof stack across all THREE layers, stripped at once.
    # = no-api-proof (edwards.rs::decompress + montgomery.rs::to_edwards proof
    #   bodies stripped; all 10 decompress-path lemmas deleted across
    #   decompress_lemmas.rs + curve_equation_lemmas.rs)
    # + the ristretto rung (ristretto.rs decompress + step_1 + step_2 proofs
    #   stripped).
    # The agent reconstructs the ENTIRE decompress proof tree — three API
    # orchestration proofs (ristretto/edwards/montgomery) + the two ristretto
    # step proofs + all 10 deleted lemmas — from the five frozen API contracts +
    # frozen specs alone. The Montgomery↔Edwards map (decompress_bridge_specs.rs),
    # every specs/* vocabulary, ristretto_lemmas/* (incl. axioms), and the whole
    # field/number-theory substrate stay FROZEN by the file guard. Each editable
    # file's fn headers are snapshotted by the spec gate, so all five API
    # contracts are frozen (SPEC_DRIFT on any header/requires/ensures edit).
    # Editable: RISTRETTO, ANCHOR(edwards), MONT, DEP(decompress_lemmas), CURVE.
    #
    # 0) reset the files not covered by the generic prep (which reset ANCHOR+DEP):
    #    MONT + RISTRETTO (editable, about to be stripped) and BRIDGE (frozen map —
    #    a leftover edit would trip FROZEN_EDIT round 1).
    for relsrc in "$MONT" "$RISTRETTO" "$BRIDGE"; do
      rel="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$relsrc" "$GITROOT")"
      git -C "$GITROOT" checkout main -- "$rel"
    done
    # 1) strip the three API proof bodies (signature + requires/ensures + exec kept)
    python3 "$HARNESS_DIR/strip_specs.py" "$ANCHOR" --in-place \
        --strip-proof-fn decompress >/dev/null
    python3 "$HARNESS_DIR/strip_specs.py" "$MONT" --in-place \
        --strip-proof-fn to_edwards >/dev/null
    python3 "$HARNESS_DIR/strip_specs.py" "$RISTRETTO" --in-place \
        --strip-proof-fn decompress \
        --strip-proof-fn step_1 \
        --strip-proof-fn step_2 >/dev/null
    # 2) delete all 10 decompress-path lemmas (same set as no-api-proof / no-bridge-lemmas)
    python3 "$HARNESS_DIR/strip_specs.py" "$DEP" --in-place \
        --delete-fn lemma_decompress_valid_branch \
        --delete-fn lemma_to_edwards_correctness \
        --delete-fn lemma_decompress_field_element_sign_bit \
        --delete-fn lemma_decompress_spec_matches_point \
        --delete-fn lemma_sign_bit_after_conditional_negate >/dev/null
    CURVE_GIT_REL="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$CURVE" "$GITROOT")"
    git -C "$GITROOT" checkout main -- "$CURVE_GIT_REL"
    python3 "$HARNESS_DIR/strip_specs.py" "$CURVE" --in-place \
        --delete-fn lemma_negation_preserves_curve \
        --delete-fn lemma_affine_to_extended_valid \
        --delete-fn lemma_edwards_affine_when_z_is_one \
        --delete-fn lemma_x_zero_implies_y_squared_one \
        --delete-fn lemma_unique_x_with_parity >/dev/null
    ;;
esac

# ── build the run.py argv (mode → experiment-mode + gate) ─────────────────────
CMD=( python3 "$HARNESS_DIR/run.py" "$TARGET"
      --project "$PROJECT"
      --run-id  "$RUN_ID"
      --rounds  "$ROUNDS"
      --max-task-minutes "$BUDGET"
      --model   "$MODEL"
      --results "$RESULTS_ROOT"
      --vstd-root "$VSTD" )
case "$MODE" in
  formal-spec)
    # proof-only: contracts given, gate ON; edit the dep only.
    CMD+=( --experiment-allow-edit "$DEP" --experiment-mode proof-only ) ;;
  no-anchor-proof)
    # contract-only: anchor file editable (agent rewrites decompress's proof
    # body) AND the dep editable (invents lemmas); gate ON freezes the anchor's
    # contract clauses. NOTE: no --no-spec-gate here — the frozen contract is
    # what makes this rung sound.
    CMD+=( --experiment-allow-edit "$ANCHOR" "$DEP" --experiment-mode contract-only ) ;;
  no-bridge-specs)
    # bridge-specs: ONLY the bridge module is editable; the gate stays ON and
    # run.py adds whole-crate verify + the frozen-file guard for this mode.
    CMD+=( --experiment-allow-edit "$BRIDGE" --experiment-mode bridge-specs ) ;;
  no-bridge-lemmas)
    # bridge-full: decompress_lemmas.rs AND the group-law library
    # curve_equation_lemmas.rs are editable. The map module
    # (decompress_bridge_specs.rs) is NOT — it stays frozen at clean main, so the
    # agent reconstructs only proofs. The gate stays ON (freezes every surviving
    # contract) and run.py adds the same whole-crate verify + frozen-file guard
    # as bridge-specs (which now also protects the frozen map module).
    CMD+=( --experiment-allow-edit "$DEP" "$CURVE" --experiment-mode bridge-full ) ;;
  no-api-proof)
    # Four files editable: the two API files (decompress / to_edwards proofs) +
    # the two lemma files. Map module + vocabulary stay frozen. run.py snapshots
    # every allow-edit file, so decompress's AND to_edwards's contracts are
    # gate-frozen even though their files are editable.
    CMD+=( --experiment-allow-edit "$ANCHOR" "$MONT" "$DEP" "$CURVE" --experiment-mode bridge-full ) ;;
  no-ristretto-proof)
    # ONE file editable: ristretto.rs (the decompress + step_1 + step_2 proofs).
    # Everything else — the edwards/montgomery/field substrate, the ristretto
    # spec vocabulary (ristretto_specs.rs) and the ristretto lemma layer
    # (ristretto_lemmas/*, incl. axioms) — stays frozen by the file guard.
    # run.py snapshots ristretto.rs's fn headers, so decompress's AND
    # step_1/step_2's contracts are gate-frozen even though the file is editable.
    CMD+=( --experiment-allow-edit "$RISTRETTO" --experiment-mode bridge-full ) ;;
  no-fullstack-proof)
    # FIVE files editable: the three API files (ristretto decompress+step_1+step_2,
    # edwards decompress, montgomery to_edwards proofs) + the two edwards lemma
    # files (10 deleted decompress-path lemmas). Map module + every specs/*
    # vocabulary + ristretto_lemmas/* (incl. axioms) + the field substrate stay
    # frozen by the file guard. run.py snapshots every allow-edit file, so all five
    # API contracts are gate-frozen even though their files are editable. Target is
    # ristretto.rs (topmost anchor); run.py's prompt branch detects the >1-file
    # editable set and renders the full-stack reconstruction prompt.
    CMD+=( --experiment-allow-edit "$RISTRETTO" "$ANCHOR" "$MONT" "$DEP" "$CURVE" --experiment-mode bridge-full ) ;;
  *)
    # no-spec & no-lemmas: agent (re)writes contracts, so the gate is OFF.
    CMD+=( --experiment-allow-edit "$DEP" --experiment-mode spec-proof --no-spec-gate ) ;;
esac

# target_id = the target file stem (run.py: target_id_from_path = path.stem):
# "edwards" for the edwards rungs, "ristretto" for --no-ristretto-proof and
# --no-fullstack-proof.
TARGET_ID="$(basename "$TARGET" .rs)"
RESULTS_DIR="$RESULTS_ROOT/$RUN_ID/$TARGET_ID"
LOG="$HARNESS_DIR/launcher_${RUN_ID}.log"

# ── detach: re-exec via Python start_new_session (POSIX setsid) so the run
# survives the caller's process-group teardown. Same mechanism as launch.sh.
export _DEMO_LOG="$LOG"
PID=$(cd "$HARNESS_DIR" && python3 - "${CMD[@]}" <<'PYEOF'
import os, subprocess, sys
p = subprocess.Popen(
    sys.argv[1:],
    stdin=subprocess.DEVNULL,
    stdout=open(os.environ['_DEMO_LOG'], 'w'),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(p.pid)
PYEOF
)

echo "$PID" > "${LOG%.log}.pid"
# hand the worktree lock from this (exiting) shell to the live run.py pid, so
# the busy-check tracks the actual run and reclaims only once it exits.
echo "$PID" > "$LOCK/pid"

# ── machine-readable handoff to the website ──────────────────────────────────
MODE_LABEL="$MODE"; [ "$STRIP_DOCS" = "1" ] && MODE_LABEL="$MODE+strip-docs"
echo "RUN_ID $RUN_ID"
echo "RESULTS $RESULTS_DIR"
echo "MODE $MODE_LABEL"
echo "LOG $LOG"
echo "PID $PID"
exit 0
