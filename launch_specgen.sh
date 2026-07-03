#!/usr/bin/env bash
# launch_specgen.sh — launch a spec-gen (spec-strip / proof-reconstruction)
# experiment run, with the STRIPPING SURFACE stated up front as data.
#
# This is the general-purpose sibling of demo_decompress.sh. Same rungs, same
# strip operations, but: (a) the per-rung surface is declarative and is PRINTED
# before every launch, (b) you can inspect it without launching (--print-surface
# / --dry-run), and (c) none of the website-demo machinery (worktree lock,
# forced detach, RUN_ID/RESULTS stdout protocol) is here.
#
# ─────────────────────────────────────────────────────────────────────────────
# WHAT "STRIPPING SURFACE" MEANS
# ─────────────────────────────────────────────────────────────────────────────
# A spec-gen rung takes a clean, fully-proven curve25519-dalek and removes one
# slice of the proof, freezing everything else. The surface of a rung is fully
# described by six facts, which this script prints for you:
#
#   TARGET     the run.py anchor file (the contract under test)
#   EXP-MODE   run.py --experiment-mode (spec-proof|proof-only|contract-only|
#              bridge-specs|bridge-full)
#   GATE       spec-integrity gate ON (contracts frozen) or OFF (agent writes
#              contracts).  ON  ⇒ any fn-header/requires/ensures edit ⇒ SPEC_DRIFT.
#   EDITABLE   files the agent may edit (run.py --experiment-allow-edit). Every
#              other file in the crate is frozen by the file guard (bridge modes)
#              or simply not handed to the agent.
#   FROZEN*    files this script explicitly resets to clean `main` and that the
#              agent must NOT touch (informational; the guard/handoff enforces it).
#   OPS        the exact edits this script makes to build the starting state:
#                admit  FILE                — replace proof-fn BODIES with admit()
#                                              (keeps contracts + docs)
#                strip-headers FILE [docs]  — delete requires/ensures/decreases
#                                              from every fn header (+ /// docs)
#                strip-proof FILE  fn…       — remove a fn's inline proof{}/assert
#                                              (keeps signature + contract + exec)
#                delete  FILE  lemma…        — delete whole lemmas (sig+contract+body)
#                delete-all-proof-fns FILE   — delete EVERY `proof fn` in the file
#
# Before any OPS run, every EDITABLE and FROZEN file is `git checkout main --`'d,
# so the prep is idempotent and never inherits a prior run's reconstruction.
#
# ─────────────────────────────────────────────────────────────────────────────
# RUNGS (increasing difficulty) — see docs/spec_gen_experiment_design.md
# ─────────────────────────────────────────────────────────────────────────────
#   --formal-spec        proofs only           (contracts given)
#   --no-spec            contract + proofs      (headers stripped; docs kept)
#   --no-spec --strip-docs   contract + proofs  (headers AND /// docs stripped)
#   --no-lemmas          invent the helpers     (every dep lemma deleted)
#   --no-anchor-proof    anchor proof + helpers (anchor proof body editable)
#   --no-bridge-specs    rebuild the Mont↔Edw map
#   --no-bridge-lemmas   rebuild the decompress lemma tree (map frozen)
#   --no-api-proof       + the two API proof bodies (edwards+montgomery)
#   --no-ristretto-proof one layer up: ristretto decompress proof layer
#   --no-fullstack-proof whole 3-layer decompress proof tree at once
#   --strip-to-fields    DEFAULT — the maximal cut: freeze the entire spec
#                        vocabulary + field/number-theory substrate + every
#                        axiom; delete EVERY non-axiom proof above the field
#                        layer (all L3 correctness lemmas) and strip the public
#                        API files' inline proofs. The agent reconstructs the
#                        whole above-field proof tree. Much larger than the
#                        decompress rungs and not structurally as tight (see the
#                        NOTE that --print-surface prints); likely infeasible in
#                        one session, kept as the headline experiment.
#
# >>> The only rung in active use is --strip-to-fields. The nine decompress
# >>> rungs above are kept for reference / the website demo ladder. <<<
#
# Usage:
#   ./launch_specgen.sh --strip-to-fields --print-surface       # the default rung — inspect the cut
#   ./launch_specgen.sh --strip-to-fields --run-id sg_001        # prep + launch (needs a clean git worktree)
#   ./launch_specgen.sh --strip-to-fields --run-id sg_002 --dry-run --detach
#   ./launch_specgen.sh --no-spec --run-id sg_003               # a reference decompress rung
#   ./launch_specgen.sh --no-bridge-lemmas --print-surface
#
# Clean start (every launch / --dry-run):
#   Before any strip, the script GUARANTEES a pristine worktree:
#     • $GITROOT a valid worktree at $SRCREF → hard-reset the member to the ref
#       and drop stray untracked files (no run inherits a prior reconstruction);
#     • $GITROOT missing → `git worktree add` it from $DALEK_SRCREPO @ $SRCREF;
#     • $GITROOT present but broken (no/corrupt .git) → re-run with --bootstrap
#       to `rm -rf` and recreate it (guarded so we never nuke a tree we own);
#     • no usable source → die with the exact command to create one.
#   So a gutted/half-stripped tree no longer wedges the run — it self-heals.
#
# Env overrides (defaults verified for this machine; DALEK_* names as demo_decompress.sh):
#   DALEK_UV_PY_BIN  DALEK_VERUS_DIR  DALEK_PROJECT  DALEK_GITROOT  DALEK_VSTD
#   DALEK_RESULTS    CLAUDE_CODE_OAUTH_TOKEN (or DALEK_DEMO_TOKEN_FILE)
#   DALEK_SRCREPO    canonical dalek-lite repo to bootstrap $GITROOT from
#   DALEK_SRCREF     clean proven ref to reset/checkout (default: main)
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── machine-specific defaults (override via env) ─────────────────────────────
UV_PY_BIN="${DALEK_UV_PY_BIN:-/path/to/python3/bin}"
VERUS_DIR="${DALEK_VERUS_DIR:-/tmp/verus-rel/verus-arm64-macos}"
PROJECT="${DALEK_PROJECT:-/private/tmp/dalek-spec-strip/curve25519-dalek}"
GITROOT="${DALEK_GITROOT:-/private/tmp/dalek-spec-strip}"
VSTD="${DALEK_VSTD:-/path/to/verus/vstd}"
RESULTS_ROOT="${DALEK_RESULTS:-$HARNESS_DIR/results}"
# The clean, fully-proven ref every run resets to (the strip starts from here).
SRCREF="${DALEK_SRCREF:-main}"
# The canonical dalek-lite git repo to (re)create $GITROOT from when it is
# missing or broken. Empty = no auto-bootstrap (the run dies with instructions
# instead of guessing). The machine default points at the local proof repo;
# override for a different clone:  DALEK_SRCREPO=/path/to/dalek-lite
SRCREPO="${DALEK_SRCREPO:-/private/tmp/dalek-baf}"

# ── the strippable files (keys used by the surface tables below) ─────────────
#   KEY          path
#   ANCHOR       src/edwards.rs                                  (edwards API; decompress)
#   MONT         src/montgomery.rs                               (montgomery API; to_edwards)
#   RISTRETTO    src/ristretto.rs                                (ristretto API; decompress/step_1/step_2)
#   DEP          src/lemmas/edwards_lemmas/decompress_lemmas.rs  (decompress helper lemmas)
#   CURVE        src/lemmas/edwards_lemmas/curve_equation_lemmas.rs (group-law lemmas)
#   BRIDGE       src/specs/decompress_bridge_specs.rs            (Mont↔Edw map; open spec fns)
path_of() {
  case "$1" in
    ANCHOR)    echo "$PROJECT/src/edwards.rs" ;;
    MONT)      echo "$PROJECT/src/montgomery.rs" ;;
    RISTRETTO) echo "$PROJECT/src/ristretto.rs" ;;
    DEP)       echo "$PROJECT/src/lemmas/edwards_lemmas/decompress_lemmas.rs" ;;
    CURVE)     echo "$PROJECT/src/lemmas/edwards_lemmas/curve_equation_lemmas.rs" ;;
    BRIDGE)    echo "$PROJECT/src/specs/decompress_bridge_specs.rs" ;;
    *) die "unknown file key: $1" ;;
  esac
}

# The 10 decompress-path lemmas deleted by the bridge-full rungs, split by file.
DECOMP_LEMMAS_DEP="lemma_decompress_valid_branch lemma_to_edwards_correctness lemma_decompress_field_element_sign_bit lemma_decompress_spec_matches_point lemma_sign_bit_after_conditional_negate"
DECOMP_LEMMAS_CURVE="lemma_negation_preserves_curve lemma_affine_to_extended_valid lemma_edwards_affine_when_z_is_one lemma_x_zero_implies_y_squared_one lemma_unique_x_with_parity"
# The 3 decompress-only helpers deleted by --no-anchor-proof (the rest stay,
# still referenced externally, so the crate keeps compiling).
ANCHOR_ONLY_LEMMAS="lemma_decompress_valid_branch lemma_decompress_field_element_sign_bit lemma_sign_bit_after_conditional_negate"

# ── the --strip-to-fields layer boundary (paths relative to $PROJECT/src) ─────
# This is the DEFAULT rung (see header). "Freeze everything reachable from the
# user-facing API contracts down to the field floor; delete every proof above it."
#   DELETE non-axiom proofs : the L3 correctness-lemma dirs (agent rebuilds them)
#   STRIP inline proofs     : the L1 public-API exec files (contract+exec kept)
#   FREEZE (reset+fileguard): L2 spec vocabulary, L4 field, L5 number-theory
#                             floor, the backend, and EVERY axiom_* (unprovable)
STF_DEL_DIRS="lemmas/edwards_lemmas lemmas/ristretto_lemmas lemmas/scalar_lemmas_ lemmas/scalar_byte_lemmas"
STF_STRIP_FILES="edwards.rs montgomery.rs ristretto.rs scalar.rs"
STF_FREEZE_DIRS="specs lemmas/field_lemmas lemmas/common_lemmas backend"

die() { echo "launch_specgen: $*" >&2; exit 2; }

# Editable lemma files for --strip-to-fields: every .rs under the delete-dirs
# EXCEPT pure-axiom files (axioms.rs stays frozen — axioms can't be rebuilt).
stf_lemma_files() {
  local d f
  for d in $STF_DEL_DIRS; do
    [ -d "$PROJECT/src/$d" ] || continue
    find "$PROJECT/src/$d" -name '*.rs' 2>/dev/null | sort | while IFS= read -r f; do
      case "$(basename "$f")" in axioms.rs|mod.rs) continue ;; esac
      echo "$f"
    done
  done
}
stf_api_files() { local f; for f in $STF_STRIP_FILES; do [ -f "$PROJECT/src/$f" ] && echo "$PROJECT/src/$f"; done; }

# Delete every NON-axiom `proof fn` from a file (keeps axiom_* and spec fns).
# Set -e safe: returns 0 even when the file has no proof fns (e.g. mod.rs).
stf_delete_nonaxiom_proofs() {
  local args=( "$1" --in-place ) n found=0
  while IFS= read -r n; do
    case "$n" in axiom_*) continue ;; esac
    args+=( --delete-fn "$n" ); found=1
  done < <(grep -oE 'proof fn [a-zA-Z0-9_]+' "$1" | awk '{print $3}')
  if [ "$found" = 1 ]; then
    python3 "$HARNESS_DIR/strip_specs.py" "${args[@]}" >/dev/null
  fi
}
# Strip inline proof content (proof{} blocks + standalone asserts) from EVERY fn
# in a file; keeps signatures, contracts, and executable bodies. No-op per fn
# that has no proof content. Guarded so an fn-less file can't fall through to
# strip_specs' default (header-strip) mode.
stf_strip_all_proofs() {
  local args=( "$1" --in-place ) n found=0
  while IFS= read -r n; do args+=( --strip-proof-fn "$n" ); found=1; done < <(
    grep -oE '\bfn [a-zA-Z0-9_]+' "$1" | awk '{print $2}' | sort -u)
  if [ "$found" = 1 ]; then
    python3 "$HARNESS_DIR/strip_specs.py" "${args[@]}" >/dev/null
  fi
}
usage() { sed -n '2,76p' "$0"; exit "${1:-0}"; }

# ─────────────────────────────────────────────────────────────────────────────
# define_surface MODE  →  sets SURF_* globals describing the rung's strip surface
# ─────────────────────────────────────────────────────────────────────────────
define_surface() {
  SURF_KIND="files"   # "files" = per-file key model; "dirs" = directory-cut rung
  SURF_EXP_MODE=""; SURF_GATE=""; SURF_TARGET_KEY=""
  SURF_EDITABLE=(); SURF_FROZEN=(); SURF_OPS=()
  case "$1" in
    formal-spec)
      SURF_EXP_MODE="proof-only"; SURF_GATE="on"; SURF_TARGET_KEY="ANCHOR"
      SURF_EDITABLE=(DEP)
      SURF_OPS+=("admit DEP")
      ;;
    no-spec)
      SURF_EXP_MODE="spec-proof"; SURF_GATE="off"; SURF_TARGET_KEY="ANCHOR"
      SURF_EDITABLE=(DEP)
      if [ "$STRIP_DOCS" = "1" ]; then SURF_OPS+=("strip-headers DEP docs"); else SURF_OPS+=("strip-headers DEP"); fi
      SURF_OPS+=("admit DEP")
      ;;
    no-lemmas)
      SURF_EXP_MODE="spec-proof"; SURF_GATE="off"; SURF_TARGET_KEY="ANCHOR"
      SURF_EDITABLE=(DEP)
      SURF_OPS+=("delete-all-proof-fns DEP")
      ;;
    no-anchor-proof)
      SURF_EXP_MODE="contract-only"; SURF_GATE="on"; SURF_TARGET_KEY="ANCHOR"
      SURF_EDITABLE=(ANCHOR DEP)
      SURF_OPS+=("strip-proof ANCHOR decompress")
      SURF_OPS+=("delete DEP $ANCHOR_ONLY_LEMMAS")
      ;;
    no-bridge-specs)
      SURF_EXP_MODE="bridge-specs"; SURF_GATE="on"; SURF_TARGET_KEY="ANCHOR"
      SURF_EDITABLE=(BRIDGE)
      SURF_FROZEN=(ANCHOR DEP)   # frozen consumers that pin the map
      SURF_OPS+=("delete BRIDGE montgomery_to_edwards_affine edwards_y_from_montgomery_u")
      ;;
    no-bridge-lemmas)
      SURF_EXP_MODE="bridge-full"; SURF_GATE="on"; SURF_TARGET_KEY="ANCHOR"
      SURF_EDITABLE=(DEP CURVE)
      SURF_FROZEN=(BRIDGE)       # the Mont↔Edw map stays frozen — proofs only
      SURF_OPS+=("delete DEP $DECOMP_LEMMAS_DEP")
      SURF_OPS+=("delete CURVE $DECOMP_LEMMAS_CURVE")
      ;;
    no-api-proof)
      SURF_EXP_MODE="bridge-full"; SURF_GATE="on"; SURF_TARGET_KEY="ANCHOR"
      SURF_EDITABLE=(ANCHOR MONT DEP CURVE)
      SURF_FROZEN=(BRIDGE)
      SURF_OPS+=("strip-proof ANCHOR decompress")
      SURF_OPS+=("strip-proof MONT to_edwards")
      SURF_OPS+=("delete DEP $DECOMP_LEMMAS_DEP")
      SURF_OPS+=("delete CURVE $DECOMP_LEMMAS_CURVE")
      ;;
    no-ristretto-proof)
      SURF_EXP_MODE="bridge-full"; SURF_GATE="on"; SURF_TARGET_KEY="RISTRETTO"
      SURF_EDITABLE=(RISTRETTO)
      # the entire edwards/montgomery/field substrate + ristretto specs + lemmas
      # (incl. axioms) stay frozen by the file guard — no --delete-fn here.
      SURF_OPS+=("strip-proof RISTRETTO decompress step_1 step_2")
      ;;
    no-fullstack-proof)
      SURF_EXP_MODE="bridge-full"; SURF_GATE="on"; SURF_TARGET_KEY="RISTRETTO"
      SURF_EDITABLE=(RISTRETTO ANCHOR MONT DEP CURVE)
      SURF_FROZEN=(BRIDGE)
      SURF_OPS+=("strip-proof RISTRETTO decompress step_1 step_2")
      SURF_OPS+=("strip-proof ANCHOR decompress")
      SURF_OPS+=("strip-proof MONT to_edwards")
      SURF_OPS+=("delete DEP $DECOMP_LEMMAS_DEP")
      SURF_OPS+=("delete CURVE $DECOMP_LEMMAS_CURVE")
      ;;
    strip-to-fields)
      # DEFAULT rung — the maximal cut. Freeze the whole spec vocabulary + field
      # substrate + number-theory floor + every axiom; delete EVERY non-axiom
      # proof above the field layer and strip the API files' inline proofs. The
      # agent reconstructs the entire above-field proof tree. Directory-granular:
      # the concrete file set is resolved from $PROJECT at prep time.
      SURF_KIND="dirs"
      SURF_EXP_MODE="bridge-full"; SURF_GATE="on"; SURF_TARGET_KEY="RISTRETTO"
      ;;
    *) die "unknown mode: $1" ;;
  esac
}

print_surface() {
  local tgt; tgt="$(path_of "$SURF_TARGET_KEY")"
  echo "──────────────────────────────────────────────────────────────────────"
  echo " RUNG       --$MODE${STRIP_DOCS:+ (strip-docs=$STRIP_DOCS)}"
  echo " TARGET     ${tgt#$PROJECT/}   [anchor / contract under test]"
  echo " EXP-MODE   $SURF_EXP_MODE"
  echo " GATE       $SURF_GATE   $([ "$SURF_GATE" = on ] && echo '(contracts frozen — header/requires/ensures edit ⇒ SPEC_DRIFT)' || echo '(agent writes contracts)')"
  if [ "$SURF_KIND" = "dirs" ]; then print_surface_dirs; return; fi
  echo -n " EDITABLE  "; for k in "${SURF_EDITABLE[@]}"; do echo -n " $(path_of "$k" | sed "s#$PROJECT/##")"; done; echo
  if [ "${#SURF_FROZEN[@]}" -gt 0 ]; then
    echo -n " FROZEN*   "; for k in "${SURF_FROZEN[@]}"; do echo -n " $(path_of "$k" | sed "s#$PROJECT/##")"; done
    echo "   [reset to main; agent must not touch]"
  fi
  echo " OPS (strip operations applied to build the starting state):"
  local op key rest
  for op in "${SURF_OPS[@]}"; do
    set -- $op; key="$2"; rest="${op#"$1 $2"}"
    case "$1" in
      admit)                 printf "   • admit proof-fn bodies        %s\n" "$(path_of "$key" | sed "s#$PROJECT/##")" ;;
      strip-headers)         printf "   • strip fn-header contracts%s  %s\n" "$([ "$rest" = ' docs' ] && echo '+docs' || echo '     ')" "$(path_of "$key" | sed "s#$PROJECT/##")" ;;
      strip-proof)           printf "   • strip inline proof of fns    %s  →%s\n" "$(path_of "$key" | sed "s#$PROJECT/##")" "$rest" ;;
      delete)                printf "   • DELETE lemmas                %s  →%s\n" "$(path_of "$key" | sed "s#$PROJECT/##")" "$rest" ;;
      delete-all-proof-fns)  printf "   • DELETE every proof fn        %s\n" "$(path_of "$key" | sed "s#$PROJECT/##")" ;;
    esac
  done
  echo "──────────────────────────────────────────────────────────────────────"
}

# Directory-cut surface (only --strip-to-fields). Described at directory
# granularity — the natural unit for a cut this broad. Concrete file counts are
# shown when $PROJECT is present.
print_surface_dirs() {
  local d nf np p
  echo " DELETE     every non-axiom \`proof fn\` (→ EDITABLE; agent rebuilds):"
  for d in $STF_DEL_DIRS; do
    if [ -d "$PROJECT/src/$d" ]; then
      nf=$(stf_lemma_files | grep -c "/src/$d/" || true)
      np=$(stf_lemma_files | grep "/src/$d/" | xargs -I{} grep -hE 'proof fn ' {} 2>/dev/null | grep -vc 'proof fn axiom_' || true)
      printf "              %-26s  (%s files, %s non-axiom proofs)\n" "src/$d/" "$nf" "$np"
    else
      printf "              %-26s  (not present in this project)\n" "src/$d/"
    fi
  done
  echo " STRIP      inline proofs, keep contract+exec (→ EDITABLE):"
  echo -n "             "; for p in $STF_STRIP_FILES; do [ -f "$PROJECT/src/$p" ] && echo -n " src/$p"; done; echo
  echo " FREEZE     reset to main + file-guard (agent must not touch):"
  echo -n "             "; for d in $STF_FREEZE_DIRS; do [ -d "$PROJECT/src/$d" ] && echo -n " src/$d/"; done
  echo "  + every axiom_* (unprovable)"
  echo " NOTE       spec fns co-located in editable lemma/API files are frozen"
  echo "            DEFINITION-DEEP: run.py runs the spec gate with --check-spec-defs,"
  echo "            so changing any existing spec fn BODY ⇒ SPEC_DRIFT. New spec"
  echo "            helpers are allowed; existing vocabulary is structurally frozen."
  echo "──────────────────────────────────────────────────────────────────────"
}

# A git that tolerates $GITROOT living in a world-writable dir (/private/tmp).
gitw() { git -C "$GITROOT" -c safe.directory="$GITROOT" "$@"; }

# Is $GITROOT a valid git worktree/repo whose $SRCREF resolves?
worktree_ok() {
  gitw rev-parse --git-dir >/dev/null 2>&1 \
    && gitw rev-parse --verify --quiet "$SRCREF" >/dev/null 2>&1
}

# git-reset a file key to the clean source ref (idempotent prep)
reset_to_main() {
  local p rel; p="$(path_of "$1")"
  rel="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$p" "$GITROOT")"
  gitw checkout "$SRCREF" -- "$rel"
}
# git-reset an absolute path (file OR dir) to the clean source ref.
reset_path_to_main() {
  local rel
  rel="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$1" "$GITROOT")"
  gitw checkout "$SRCREF" -- "$rel"
}

# ── Guarantee a CLEAN starting state before any strip ────────────────────────
# Three cases, in order:
#   1. $GITROOT is a valid worktree at $SRCREF → HARD-reset the member to the
#      ref and remove stray untracked files (a prior run's reconstruction), so
#      every run starts pristine — not just the files this rung happens to touch.
#   2. $GITROOT is missing/broken AND $SRCREPO is a valid repo → (re)create the
#      worktree from the source. A broken-but-present $GITROOT needs --bootstrap
#      (it gets `rm -rf`'d), so we never silently delete a tree we didn't make.
#   3. otherwise → die with the exact command to fix it.
ensure_clean_worktree() {
  local member_rel
  if worktree_ok; then
    member_rel="$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1],sys.argv[2]))' "$PROJECT" "$GITROOT")"
    echo "launch_specgen: hard-reset $member_rel → $SRCREF (clean start)…" >&2
    gitw checkout --force "$SRCREF" -- "$member_rel" \
      || die "git checkout $SRCREF failed in $GITROOT (ref present but checkout errored)"
    # drop stray untracked files under src/ (keep ignored build output: no -x)
    gitw clean -fd -- "$member_rel/src" >/dev/null
    return 0
  fi

  # $GITROOT is not usable. Can we rebuild it?
  if [ -z "$SRCREPO" ]; then
    die "$(cat <<EOF
worktree not clean-startable: $GITROOT is not a git worktree at ref '$SRCREF'.
  (a stale/gutted checkout has no .git, or its .git is corrupt — see the run report)
Fix one of:
  • create it from the canonical proof repo:
      python admit.py --worktree "$GITROOT" --gitroot /path/to/dalek-lite --ref $SRCREF
  • or set DALEK_SRCREPO=/path/to/dalek-lite and re-run with --bootstrap
EOF
)"
  fi
  local srcgit=(git -C "$SRCREPO" -c safe.directory="$SRCREPO")
  "${srcgit[@]}" rev-parse --verify --quiet "$SRCREF" >/dev/null 2>&1 \
    || die "DALEK_SRCREPO=$SRCREPO has no ref '$SRCREF' (set DALEK_SRCREF or fix the repo)"
  if [ -e "$GITROOT" ] && [ "$BOOTSTRAP" != 1 ]; then
    die "$GITROOT exists but isn't a valid worktree. Re-run with --bootstrap to rm -rf and recreate it from $SRCREPO, or remove it yourself."
  fi
  echo "launch_specgen: bootstrapping worktree $GITROOT from $SRCREPO @ $SRCREF…" >&2
  [ -e "$GITROOT" ] && rm -rf "$GITROOT"
  "${srcgit[@]}" worktree prune >/dev/null 2>&1 || true
  "${srcgit[@]}" worktree add --force --detach "$GITROOT" "$SRCREF" \
    || die "git worktree add failed (source $SRCREPO, ref $SRCREF)"
}

# Prep for the directory-cut rung: reset all touched paths to main, then delete
# above-field proofs and strip the API files. Resolves files from $PROJECT.
apply_strip_to_fields() {
  local f d
  echo "launch_specgen: deleting non-axiom proofs above the field layer…" >&2
  while IFS= read -r f; do stf_delete_nonaxiom_proofs "$f"; done < <(stf_lemma_files)
  echo "launch_specgen: stripping inline proofs from the API exec files…" >&2
  for f in $(stf_api_files); do stf_strip_all_proofs "$f"; done
}

apply_ops() {
  local op key rest p
  for op in "${SURF_OPS[@]}"; do
    set -- $op; key="$2"; p="$(path_of "$key")"; rest="${op#"$1 $2"}"
    case "$1" in
      admit)
        python3 "$HARNESS_DIR/admit.py" "$p" --in-place --mode fn-bodies >/dev/null ;;
      strip-headers)
        local sa=( "$p" --in-place ); [ "$rest" = ' docs' ] && sa+=( --strip-docs )
        python3 "$HARNESS_DIR/strip_specs.py" "${sa[@]}" >/dev/null ;;
      strip-proof)
        local sp=( "$p" --in-place ); for fn in $rest; do sp+=( --strip-proof-fn "$fn" ); done
        python3 "$HARNESS_DIR/strip_specs.py" "${sp[@]}" >/dev/null ;;
      delete)
        local dl=( "$p" --in-place ); for fn in $rest; do dl+=( --delete-fn "$fn" ); done
        python3 "$HARNESS_DIR/strip_specs.py" "${dl[@]}" >/dev/null ;;
      delete-all-proof-fns)
        local da=( "$p" --in-place )
        while IFS= read -r fn; do da+=( --delete-fn "$fn" ); done < <(
          grep -oE 'proof fn [a-zA-Z0-9_]+' "$p" | awk '{print $3}')
        python3 "$HARNESS_DIR/strip_specs.py" "${da[@]}" >/dev/null ;;
    esac
  done
}

# ── args ─────────────────────────────────────────────────────────────────────
MODE=""; STRIP_DOCS=0; RUN_ID=""; MODEL="opus"
ROUNDS=4; BUDGET=45; ROUNDS_SET=0; BUDGET_SET=0
PRINT_ONLY=0; DRY_RUN=0; DETACH=0; SKIP_WARM=0; BOOTSTRAP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --formal-spec|--no-spec|--no-lemmas|--no-anchor-proof|--no-bridge-specs|\
    --no-bridge-lemmas|--no-api-proof|--no-ristretto-proof|--no-fullstack-proof|\
    --strip-to-fields)
      MODE="${1#--}"; shift ;;
    --strip-docs)     STRIP_DOCS=1; shift ;;
    --run-id)         RUN_ID="$2"; shift 2 ;;
    --rounds)         ROUNDS="$2"; ROUNDS_SET=1; shift 2 ;;
    --budget)         BUDGET="$2"; BUDGET_SET=1; shift 2 ;;
    --model)          MODEL="$2"; shift 2 ;;
    --print-surface)  PRINT_ONLY=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --detach)         DETACH=1; shift ;;
    --skip-warm)      SKIP_WARM=1; shift ;;
    --bootstrap)      BOOTSTRAP=1; shift ;;
    -h|--help)        usage 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

[ -n "$MODE" ] || die "mode required (one of --formal-spec --no-spec --no-lemmas --no-anchor-proof --no-bridge-specs --no-bridge-lemmas --no-api-proof --no-ristretto-proof --no-fullstack-proof)"
if [ "$STRIP_DOCS" = "1" ] && [ "$MODE" != "no-spec" ]; then
  die "--strip-docs only applies to --no-spec"
fi

# per-rung default headroom (overridable). Mirrors demo_decompress.sh.
case "$MODE" in
  no-anchor-proof)   [ "$ROUNDS_SET" = 1 ] || ROUNDS=6;  [ "$BUDGET_SET" = 1 ] || BUDGET=90  ;;
  no-bridge-specs)   [ "$ROUNDS_SET" = 1 ] || ROUNDS=7;  [ "$BUDGET_SET" = 1 ] || BUDGET=120 ;;
  no-bridge-lemmas)  [ "$ROUNDS_SET" = 1 ] || ROUNDS=10; [ "$BUDGET_SET" = 1 ] || BUDGET=180 ;;
  no-api-proof|no-ristretto-proof) [ "$ROUNDS_SET" = 1 ] || ROUNDS=12; [ "$BUDGET_SET" = 1 ] || BUDGET=240 ;;
  no-fullstack-proof) [ "$ROUNDS_SET" = 1 ] || ROUNDS=16; [ "$BUDGET_SET" = 1 ] || BUDGET=240 ;;
  # the maximal cut — most headroom, still under the 5-hour session window.
  strip-to-fields)    [ "$ROUNDS_SET" = 1 ] || ROUNDS=20; [ "$BUDGET_SET" = 1 ] || BUDGET=240 ;;
esac

define_surface "$MODE"

# --print-surface: print the plan and stop (no worktree needed; counts show
# "not present" if $PROJECT isn't checked out yet — that's expected here).
if [ "$PRINT_ONLY" = 1 ]; then print_surface; exit 0; fi
[ -n "$RUN_ID" ] || die "--run-id required (omit only with --print-surface)"

# ── env prelude ──────────────────────────────────────────────────────────────
export PATH="$UV_PY_BIN:$VERUS_DIR:$PATH"
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -n "${DALEK_DEMO_TOKEN_FILE:-}" ] && [ -f "$DALEK_DEMO_TOKEN_FILE" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat "$DALEK_DEMO_TOKEN_FILE")"; export CLAUDE_CODE_OAUTH_TOKEN
fi

# ── preflight ────────────────────────────────────────────────────────────────
command -v python3     >/dev/null || die "python3 not on PATH ($UV_PY_BIN missing?)"
command -v cargo-verus >/dev/null || die "cargo-verus not on PATH ($VERUS_DIR missing?)"
command -v claude      >/dev/null || die "claude not on PATH"

# Clean start: validate / hard-reset / (re)bootstrap the worktree BEFORE any
# strip, so a run never inherits a prior reconstruction or a half-broken tree.
# (Must precede the target/PROJECT checks — on bootstrap the tree appears here.)
ensure_clean_worktree

[ -d "$PROJECT" ] || die "project dir missing after clean-start: $PROJECT (bad DALEK_PROJECT vs DALEK_GITROOT?)"
TARGET="$(path_of "$SURF_TARGET_KEY")"
[ -f "$TARGET" ] || die "target missing: $TARGET"

# Now that the worktree is present and pristine, print the surface with REAL
# file/proof counts (the cut the agent will actually face).
print_surface
mkdir -p "$RESULTS_ROOT"

# ── one-time vstd/build warm (cold module-scoped check spuriously fails) ──────
WARM_SENTINEL="$PROJECT/target/.specgen_warmed"
if [ "$SKIP_WARM" != 1 ] && [ ! -f "$WARM_SENTINEL" ]; then
  echo "launch_specgen: warming verus build (one-time, ~40s)…" >&2
  ( cd "$PROJECT" && cargo verus verify -p curve25519-dalek >/dev/null 2>&1 ) || true
  mkdir -p "$(dirname "$WARM_SENTINEL")"; : > "$WARM_SENTINEL"
fi

# ── prep: reset to clean main, then apply the strip surface ───────────────────
EDIT_PATHS=()
if [ "$SURF_KIND" = "dirs" ]; then
  apply_strip_to_fields
  while IFS= read -r f; do EDIT_PATHS+=( "$f" ); done < <(stf_lemma_files)
  for f in $(stf_api_files); do EDIT_PATHS+=( "$f" ); done
  [ "${#EDIT_PATHS[@]}" -gt 0 ] || die "no editable files resolved under $PROJECT/src (is this the right project?)"
else
  echo "launch_specgen: resetting editable+frozen files to clean main…" >&2
  for k in "${SURF_EDITABLE[@]}" ${SURF_FROZEN[@]+"${SURF_FROZEN[@]}"}; do reset_to_main "$k"; done
  echo "launch_specgen: applying strip operations…" >&2
  apply_ops
  for k in "${SURF_EDITABLE[@]}"; do EDIT_PATHS+=( "$(path_of "$k")" ); done
fi

# ── build the run.py argv ─────────────────────────────────────────────────────
CMD=( python3 "$HARNESS_DIR/run.py" "$TARGET"
      --project "$PROJECT" --run-id "$RUN_ID"
      --rounds "$ROUNDS" --max-task-minutes "$BUDGET"
      --model "$MODEL" --results "$RESULTS_ROOT" --vstd-root "$VSTD"
      --experiment-allow-edit "${EDIT_PATHS[@]}"
      --experiment-mode "$SURF_EXP_MODE" )
# spec-proof rungs let the agent (re)write contracts, so the gate must be OFF.
[ "$SURF_GATE" = "off" ] && CMD+=( --no-spec-gate )

TARGET_ID="$(basename "$TARGET" .rs)"
RESULTS_DIR="$RESULTS_ROOT/$RUN_ID/$TARGET_ID"
LOG="$HARNESS_DIR/launcher_specgen_${RUN_ID}.log"

echo "launch_specgen: run.py argv:" >&2
printf '   %q' "${CMD[@]}" >&2; echo >&2

if [ "$DRY_RUN" = 1 ]; then
  echo "launch_specgen: --dry-run — prep applied, NOT launching." >&2
  echo "DRY_RUN_RESULTS $RESULTS_DIR"
  exit 0
fi

# ── launch ───────────────────────────────────────────────────────────────────
if [ "$DETACH" = 1 ]; then
  # re-exec via Python start_new_session (POSIX setsid) so the run survives the
  # caller's process-group teardown — required when launching from Claude Code.
  export _SG_LOG="$LOG"
  PID=$(cd "$HARNESS_DIR" && python3 - "${CMD[@]}" <<'PYEOF'
import os, subprocess, sys
p = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL,
                     stdout=open(os.environ['_SG_LOG'], 'w'),
                     stderr=subprocess.STDOUT, start_new_session=True)
print(p.pid)
PYEOF
)
  echo "$PID" > "${LOG%.log}.pid"
  echo "RUN_ID $RUN_ID"
  echo "RESULTS $RESULTS_DIR"
  echo "LOG $LOG"
  echo "PID $PID"
else
  echo "RUN_ID $RUN_ID"
  echo "RESULTS $RESULTS_DIR"
  exec "${CMD[@]}"
fi
