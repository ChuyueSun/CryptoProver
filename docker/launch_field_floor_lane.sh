#!/usr/bin/env bash
#
# Operator-side launcher for one focused field-floor lane.
#
# This intentionally wraps docker/run_agents.sh instead of adding a scheduler to
# run.py. The core driver still accepts one target file, so this script records
# the lane as manifest/provenance metadata and uses the lane's primary lemma file
# as the run.py anchor while making the whole lane the editable scope.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/.." && pwd)

LANE="scalar-montgomery"
BASE_MANIFEST="$repo/peel_manifests/field_floor.json"
IMAGE="${DALEK_LANE_IMAGE:-dalek-harness:f3bfa28-fix8-lane-isolation-stubshield}"
GITROOT=""
REF=""
RUN_ID=""
WORK_BASE="${DALEK_LANE_WORK_BASE:-/srv/agents}"
SEED_WIP=""
FAILURE_MEMORY_SEED=""
OPERATOR_SEED=""
ROUNDS=14
MINUTES=180
CARGO_JOBS=4
MAX_PARALLEL=1
REQUIRE_TAP=1
PROVENANCE=""
DRY_RUN=0
LANE_SCOPE="structural"          # structural | prompt-only
THREAD=""                        # empty | lane-plan | frontier | chain-first | decomp-control | decomp-first-edit | decomp-helper | constant-dist | carry-shift | product-bound | as-bytes
PRE_EDIT_DIAGNOSTIC_BLOCK=0

usage() {
    cat <<'EOF'
Usage:
  docker/launch_field_floor_lane.sh --run-id ID --gitroot REPO --ref REF [options]

Options:
  --lane NAME            Lane to launch (currently: scalar-montgomery)
  --base-manifest PATH   Base field-floor peel manifest
  --image IMAGE          Docker image to run
  --work-base DIR        Host work/results base passed to run_agents.sh
  --seed-wip PATCH       Optional guarded WIP resume patch applied post-peel/pre-seal
  --failure-memory-seed JSON
                         Prior failure_memory.json copied into each isolated
                         agent /results before prompt render
  --operator-seed PATCH  Operator-owned source seed applied post-peel/pre-seal;
                         may touch frozen files and is non-scoreable scaffolding
  --rounds N             run.py rounds (default: 14)
  --minutes N            per-round task minutes in the manifest list (default: 180)
  --cargo-jobs N         CARGO_BUILD_JOBS per container (default: 4)
  --max-parallel N       Container concurrency (default: 1)
  --provenance TEXT      Override DALEK_EXPERIMENT_PROVENANCE
  --prompt-only-full     Keep the full field-floor editable cone and add only
                         lane metadata. For diagnostics only; not scoreable as
                         lane isolation.
  --thread NAME          Launch a stricter proof-thread packet inside the lane.
                         Currently: lane-plan | frontier | chain-first |
                         decomp-control | decomp-first-edit | decomp-helper |
                         constant-dist |
                         product-bound | carry-shift | as-bytes. lane-plan, frontier,
                         chain-first, and product-bound keep the whole lane
                         writable and pin order in NL; lane-plan uses
                         scalar.rs as the consumer/check anchor. decomp-control
                         decomp-first-edit, and decomp-helper narrow active
                         edits to the part1-chain file. as-bytes and
                         carry-shift narrow active edits to the Montgomery
                         lemma file.
  --no-require-tap       Use best-effort --tap instead of fail-closed --require-tap
  --dry-run              Write generated files and print the command, do not launch

Environment forwarded to run_agents.sh when set:
  CLAUDE_CODE_OAUTH_TOKEN, DALEK_TAP_BASE_PORT, DALEK_TAP_LIVE_BASE,
  DALEK_TAP_OUT, DALEK_TAP_LOG, DALEK_VSTD_CONTAINER
EOF
}

die() { echo "launch_field_floor_lane: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --lane) LANE="$2"; shift 2 ;;
        --base-manifest) BASE_MANIFEST="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --gitroot) GITROOT="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --run-id) RUN_ID="$2"; shift 2 ;;
        --work-base) WORK_BASE="$2"; shift 2 ;;
        --seed-wip) SEED_WIP="$2"; shift 2 ;;
        --failure-memory-seed) FAILURE_MEMORY_SEED="$2"; shift 2 ;;
        --operator-seed) OPERATOR_SEED="$2"; shift 2 ;;
        --rounds) ROUNDS="$2"; shift 2 ;;
        --minutes) MINUTES="$2"; shift 2 ;;
        --cargo-jobs) CARGO_JOBS="$2"; shift 2 ;;
        --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
        --provenance) PROVENANCE="$2"; shift 2 ;;
        --prompt-only-full) LANE_SCOPE="prompt-only"; shift ;;
        --thread) THREAD="$2"; shift 2 ;;
        --no-require-tap) REQUIRE_TAP=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done

[ "$LANE" = "scalar-montgomery" ] || die "unsupported lane: $LANE"
case "$THREAD" in
    ""|lane-plan|frontier|chain-first|decomp-control|decomposition-control|decomp-first-edit|first-bridge|decomp-helper|helper-bridge|micro-bridge|constant-dist|const-dist|l0-only|l0-micro|product-bound|carry-shift|as-bytes) ;;
    *) die "unsupported thread: $THREAD" ;;
esac
case "$THREAD" in
    constant-dist|const-dist|l0-only|l0-micro) PRE_EDIT_DIAGNOSTIC_BLOCK=1 ;;
esac
[ -n "$RUN_ID" ] || die "--run-id required"
[ -n "$GITROOT" ] || die "--gitroot required"
[ -n "$REF" ] || die "--ref required"
[ -f "$BASE_MANIFEST" ] || die "--base-manifest not found: $BASE_MANIFEST"
[ -z "$SEED_WIP" ] || [ -f "$SEED_WIP" ] || die "--seed-wip patch not found: $SEED_WIP"
[ -z "$FAILURE_MEMORY_SEED" ] || [ -f "$FAILURE_MEMORY_SEED" ] || die "--failure-memory-seed not found: $FAILURE_MEMORY_SEED"
[ -z "$OPERATOR_SEED" ] || [ -f "$OPERATOR_SEED" ] || die "--operator-seed patch not found: $OPERATOR_SEED"
git -C "$GITROOT" rev-parse --git-dir >/dev/null || die "--gitroot is not a git repo: $GITROOT"
if [ "$DRY_RUN" != "1" ]; then
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || die "CLAUDE_CODE_OAUTH_TOKEN not set"
    command -v docker >/dev/null || die "docker not on PATH"
fi

launcher_dir="$WORK_BASE/$RUN_ID/_launcher"
mkdir -p "$launcher_dir"

lane_manifest="$launcher_dir/field_floor_${LANE}.json"
manifests_file="$launcher_dir/manifests.txt"
lane_meta="$launcher_dir/lane.json"
brief_file="$launcher_dir/thread_brief.md"

python3 - "$BASE_MANIFEST" "$lane_manifest" "$lane_meta" "$brief_file" "$RUN_ID" "$LANE_SCOPE" "$THREAD" <<'PY'
import json
import sys
from pathlib import Path

base_path, out_path, meta_path, brief_path = map(Path, sys.argv[1:5])
run_id = sys.argv[5]
lane_scope = sys.argv[6]
thread = sys.argv[7]
manifest = json.loads(base_path.read_text())

lane_lemma_anchor = "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_lemmas.rs"
lane_part1_chain = "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_part1_chain_lemmas.rs"
lane_part2_chain = "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_part2_chain_lemmas.rs"
lane_anchor = lane_lemma_anchor
if thread == "lane-plan":
    lane_anchor = "curve25519-dalek/src/scalar.rs"
lane_editable = [
    lane_lemma_anchor,
    lane_part1_chain,
    lane_part2_chain,
    "curve25519-dalek/src/scalar.rs",
]
lane_read_anchors = [
    "curve25519-dalek/src/backend/serial/u64/scalar.rs",
    "curve25519-dalek/src/specs/montgomery_reduce_specs.rs",
    "curve25519-dalek/src/specs/scalar52_specs.rs",
]

entries_by_path = {entry.get("path"): entry for entry in manifest.get("files", [])}
missing = [path for path in lane_editable if path not in entries_by_path]
if missing:
    raise SystemExit(f"base manifest does not include scalar Montgomery lane files: {missing}")

if lane_scope == "structural":
    manifest["files"] = [entries_by_path[path] for path in lane_editable]
    manifest["name"] = f"{manifest.get('name', 'field-floor-cut')}::scalar-montgomery-structural-lane"
elif lane_scope == "prompt-only":
    manifest["name"] = f"{manifest.get('name', 'field-floor-cut')}::scalar-montgomery-prompt-only"
else:
    raise SystemExit(f"unsupported lane scope: {lane_scope}")

manifest["target"] = lane_anchor
active_editable = lane_editable if lane_scope == "structural" else [
    entry.get("path") for entry in manifest.get("files", [])
]
thread_brief = ""
if thread == "lane-plan":
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    manifest["name"] = manifest["name"] + "::thread-lane-plan-consumer-anchor"
    thread_brief = """Scalar-Montgomery lane scheduler for this run. This is a natural-language scheduler, not a GT lemma list and not permission to script every move.

Anchor and scope: the harness target is `scalar.rs` on purpose, because the frozen consumer/API proof surface is the best check anchor for the lane. Do not read that as "only edit scalar.rs". The writable unit is the full scalar-Montgomery lane: `montgomery_reduce_lemmas.rs`, `montgomery_reduce_part1_chain_lemmas.rs`, `montgomery_reduce_part2_chain_lemmas.rs`, and lane-relevant proof blocks in `scalar.rs`.

Allowed scalar.rs work: scalar API proof blocks, local proof-only assertions, and helper calls are allowed when they discharge `montgomery_reduce` / `from_montgomery` obligations or expose the direct lane dependency. Do not wander into arbitrary scalar API cleanup, digit/radix/NAF, Pippenger/Straus, Edwards, Ristretto, or generic scalar ergonomics unless you can write the direct dependency path back to the Montgomery reduction lane.

Order to converge: work on one proof thread at a time. First get the current lane to a usable verifier frontier: resolve missing lane-local signatures/contracts only as needed by the current consumer callsite, then rerun the same scalar/Montgomery check. From the reported errors, trace the immediate dependency chain from `scalar.rs` down into the editable Montgomery lemmas, choose the lowest unproved prerequisite whose own prerequisites are already proved or writable, and bank that one leaf before moving upward. If a proof blocks, split only its immediate prerequisite and return to the same thread.

Current priority if the verifier does not point elsewhere: stabilize the product-bound expression-shape repair that previous runs exposed, then the byte/as_bytes boundary in `lemma_as_bytes_52`, then part1-chain arithmetic/carry facts, then part2-chain bounds/carry facts, then the `scalar.rs` caller proof blocks. This is a soft order; a precise verifier span in the current lane can override it, but off-lane noise cannot.

Proof style to imitate from the GT docs, without copying GT signatures: search same file first, then common lemmas, then scalar-domain lemmas. Decompose nonlinear limb products through small named facts and scoped `assert(...) by { ... }` blocks. Pull opaque field/index/cast expressions into simple locals before `by (bit_vector)` or nonlinear assertions. Prefer several small bridge facts over one giant theorem.

Verifier rhythm: after each edit, rerun the same scoped scalar/Montgomery check or an even narrower current-lane function/module check. Never start a second verifier while an earlier verifier command from this run is still live; wait for it to finish, or explicitly kill the stale one before rerunning. A real step is one verified leaf, one removed hard admit, or one contract repair that survives the same check. A lower error count with added hard admits is compile debt, not progress; immediately return to those admits before expanding scope.

Integration rhythm: after a lane-local bank, run a deliberate broader scalar/whole-crate reconciliation only to collect traced lane seams and small caller proof-block repairs. Do not treat broad off-lane failures as the next task. A final lane signal is not COMPLETE until a de-stubbed whole-crate integration gate passes with zero non-axiom admits.
"""
elif thread == "carry-shift":
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    active_editable = [lane_lemma_anchor]
    manifest["name"] = manifest["name"] + "::thread-carry-shift"
    thread_brief = """First proof thread: bank the carry-shift arithmetic leaf in the main Montgomery reduction lemma file. The point is to prove one small low-level fact about shifts, powers of two, and nat/u128 arithmetic before touching the larger pre-sub/post-sub Montgomery story.

For this run, treat the part1-chain file, part2-chain file, and scalar.rs as peeled but inactive pins. Read them only. Do not add skeletons or admits there. Do not spread the scaffold across sibling files.

Allowed edit behavior: stay in the active main lemma file. If a compile blocker forces a helper, make it local to this file and prove it as part of the same thread. At most one temporary local admit is acceptable as compile debt for this thread, and the next action must be to remove or prove that admit. A lower error count with several new admits is not progress.

Verifier rhythm: use the scoped Montgomery lemma-module check for tight feedback. Consult the backend scalar check only as consumer signal after the active leaf compiles. Do not chase byte conversion, scalar digit/radix/NAF, Pippenger/Straus, Edwards, or Ristretto errors in this run.
"""
elif thread == "frontier":
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    manifest["name"] = manifest["name"] + "::thread-verifier-frontier"
    thread_brief = """Verifier-frontier scheduler for this run: keep the whole scalar-Montgomery lane writable, but work as if there is only one current proof thread. Do not preselect a GT lemma signature or invent a broad dependency DAG. Let the verifier output name the next obligation.

Start by running the scoped Montgomery lemma-module check and reading the exact current errors. Filter out off-lane noise, then choose the first lane-local obligation whose immediate prerequisites are either already proved or also writable in this lane. Bank that one thread before moving to the next thread.

Use the lane files as follows: the Montgomery lemma files are the main proof surface; scalar.rs is allowed for lane-tracing proof blocks, small local proof assertions, and helper calls needed by montgomery_reduce/from_montgomery. Scalar API proof blocks are allowed when they discharge this lane; arbitrary scalar API cleanup is not.

Convergence rule: after every edit, rerun the same scoped Montgomery check or a narrower lane check. A real step is one verified leaf, one removed admit, or one contract repair that survives the same check. A lower error count with new admits is compile debt, not progress. If you must add a temporary admit to regain compilation, immediately return to that exact admit and prove or remove it before expanding scope.

Order rule: fix the verifier frontier in this sequence: missing lane-local signatures/contracts; syntax/type errors in the active proof thread; arithmetic/cast facts needed by that thread; caller-facing postconditions for that same thread; then the next verifier error. Do not chase Pippenger/Straus/digit/radix/NAF, Edwards, Ristretto, or unrelated scalar errors unless you can write the direct dependency path back to this Montgomery reduction thread.

Stop condition: do not claim COMPLETE from a lane-scoped green alone. First require the scoped Montgomery lane to verify with zero new hard admits; then the operator must run the de-stubbed whole-crate integration gate before counting this as banked field-floor progress.
"""
elif thread == "chain-first":
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    manifest["name"] = manifest["name"] + "::thread-chain-first-post024"
    thread_brief = """Post-run024 scalar-Montgomery lane scheduler. This is a natural-language order pin, not a hard-coded state machine and not a GT lemma signature list. Keep the whole scalar-Montgomery lane writable: `montgomery_reduce_lemmas.rs`, `montgomery_reduce_part1_chain_lemmas.rs`, `montgomery_reduce_part2_chain_lemmas.rs`, and lane-relevant proof blocks in `scalar.rs`.

Known diagnostic from the prior lane run: the Montgomery target proof draft reached zero admits in the main target file, but the broader scalar/sibling task still had hard-admit debt and ended `SIBLING_VERUS_FAIL`. The module fan-out at termination was: package aggregate errors=1, backend scalar errors=12, main `montgomery_reduce_lemmas` errors=2, part1-chain errors=1, and part2-chain errors=1. Treat this as a map of where to work, not as evidence that the crate is one obligation from done.

Order to converge: work one proof thread at a time, bottom-up. First make the part1-chain module check clean. Then make the part2-chain module check clean. Then return to the two main Montgomery lemma-module errors. Only after those are stable should you spend effort on `scalar.rs` caller proof blocks or backend-scalar consumer diagnostics. Broad package errors are integration signal, not the next proof thread.

Scalar.rs permissions: scalar API proof blocks, local proof-only assertions, and helper calls are allowed when they discharge `montgomery_reduce` / `from_montgomery` obligations or expose the direct lane dependency. Do not wander into arbitrary scalar API cleanup, digit/radix/NAF, Pippenger/Straus, Edwards, Ristretto, or generic scalar ergonomics unless you can write the direct dependency path back to this Montgomery reduction lane.

Verifier rhythm: start by running the chain module checks and reading exact spans. After every edit, rerun the same module check for the current thread before moving upward. Never start a second verifier while an earlier verifier command is live; wait for it to finish or explicitly kill the stale one. A real step is a green current-thread module check, one removed hard admit, or one contract/caller repair that survives the same check. A lower error count with new hard admits is compile debt, not progress; immediately return to those admits.

Proof style to imitate from the GT docs, without copying GT signatures: search the same file first, then common lemmas, then scalar-domain lemmas. Decompose nonlinear limb products through small named facts and scoped `assert(...) by { ... }` blocks. Pull opaque field, array-index, cast, and spec expressions into simple locals before `by (bit_vector)` or nonlinear assertions. Prefer several small bridge facts over one giant theorem.

Stop condition: a lane-scoped green with zero new hard admits is only a lane signal. The operator still owes a de-stubbed scalar/whole-crate integration gate before counting this as field-floor progress.
"""
elif thread in ("decomp-control", "decomposition-control"):
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    lane_anchor = lane_part1_chain
    manifest["target"] = lane_anchor
    active_editable = [lane_part1_chain]
    manifest["name"] = manifest["name"] + "::thread-decomposition-control"
    thread_brief = """Operator-decomposition-guided scheduler for this run. This is a natural-language proof-method pin, not a GT lemma signature list and not a broad orchestration script.

Firewall label: this diagnostic run gives the agent the decomposition target. A success here shows the agent can execute a handed-down decomposition, not that it independently found the proof order. Do not describe this as a full field-floor result, a ceiling break, or an unguided §11-style result.

Context from the prior chain-first lane run: the scheduler correctly reached the part1-chain and part2-chain modules, but both stayed at the same single timeout/error floor even under higher rlimit. The useful signal was the agent's own diagnosis: the part1 bottleneck is `lemma_nl_decomposition`, especially the giant equality that proves `n * L_value` is the low coefficient sum plus `nl_high * P5`. More rlimit and more package/backend/scalar probing did not produce an edit.

Active edit scope: until one bridge inside `lemma_nl_decomposition` has been written and checked, edit only `montgomery_reduce_part1_chain_lemmas.rs`. The rest of the scalar-Montgomery lane is present for provenance, reading, and later accounting, but it is not writable in this first phase. Do not touch `montgomery_reduce_lemmas.rs`, `montgomery_reduce_part2_chain_lemmas.rs`, or `scalar.rs` in this run unless the operator relaunches a broader packet.

First move: do not run another Verus check as your first action. Open `montgomery_reduce_part1_chain_lemmas.rs`, locate `lemma_nl_decomposition`, and make one source edit that splits the large `n * L_value == nl_low_coeffs + nl_high * P5` proof into a named intermediate equality. Only after that edit should you run the part1-chain module check.

Concrete proof shape in natural language: expand `n * L_value` as a product of two short base-`P` polynomials; name the coefficient collection by powers `P^0` through `P^8`; name the low/high split at `P5`; then use those named bridge facts to reach the final equality. Bank one bridge at a time. The first bridge can be just the product-expansion fact or just the coefficient-collection fact; it does not need to finish the whole lemma.

Local style: use small scoped `assert(...) by { ... }` blocks and existing arithmetic/power/multiplication lemmas already in the file or nearby common/scalar lemma libraries. Keep nonlinear arithmetic goals small. Pull repeated products, powers, casts, and coefficient sums into simple locals before asking the solver to prove them. Prefer several bridge assertions over one giant `by (nonlinear_arith)` block.

Verifier rhythm: after the first source edit, run only the scoped part1-chain module check. Do not run part2-chain, main Montgomery, backend scalar, package, or whole-crate checks until the part1 edit has landed a real same-module improvement. Do not raise rlimit before making the first decomposition edit. If the part1 check still times out, split the same bridge further; do not switch proof threads.

Progress accounting: a real step is one new bridge fact in `lemma_nl_decomposition` that survives the part1-chain check, one reduced timeout span tied to that bridge, or one removed hard admit in the part1-chain file. A lower broad-package error count, a fresh rlimit timeout, or new hard admits elsewhere is not progress. COMPLETE is impossible from this phase alone; this run is meant to bank the first decomposition bridge so the lane can later climb back through part2/main/scalar integration.
"""
elif thread in ("decomp-first-edit", "first-bridge"):
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    lane_anchor = lane_part1_chain
    manifest["target"] = lane_anchor
    active_editable = [lane_part1_chain]
    manifest["name"] = manifest["name"] + "::thread-decomposition-first-edit"
    thread_brief = """Operator-first-edit-guided scheduler for this run. This is a natural-language proof-method packet, not a GT lemma signature list and not a hard-coded proof script.

Firewall label: this diagnostic run tells the agent which proof edit to make first. A success here shows the agent can continue from an operator-pinned first bridge edit, not that it independently found the field-floor order or decomposition. Do not describe this as a full field-floor result, a ceiling break, or an unguided §11-style result.

Why this packet exists: the previous operator-decomposition-guided run respected the no-Verus-before-edit rule, but stalled in read-only planning. So this run treats the first source edit itself as the task boundary. The first useful artifact is not another search result, helper inventory, package check, or rlimit retry. It is one local bridge scaffold inside `lemma_nl_decomposition`.

Active edit scope: edit only `montgomery_reduce_part1_chain_lemmas.rs` in this run. The rest of the scalar-Montgomery lane is present for provenance, reading after the first edit if needed, and later accounting; it is not writable in this first phase. Do not touch `montgomery_reduce_lemmas.rs`, `montgomery_reduce_part2_chain_lemmas.rs`, or `scalar.rs`.

First-action rule: your first tool action may read the active part1-chain file to locate `lemma_nl_decomposition`. After that, make a source edit before opening helper libraries, running Verus, searching, or inspecting broad package state. If you are unsure which bridge to write, choose the product-expansion bridge for the `n * L_value` equality. Do not wait for a perfect decomposition.

Concrete first edit in natural language: inside `lemma_nl_decomposition`, split the giant equality proving `n * L_value` equals the low coefficient sum plus `nl_high * P5`. Add one small local bridge fact that names an intermediate stage. Good first-stage names are: product expansion of two short base-`P` polynomials; collection of coefficients by powers `P^0` through `P^8`; or the low/high split at `P5`. Pick exactly one of these stages and write it as a local assertion/calc-style bridge around the existing proof expression. Keep the existing final goal nearby so the verifier points at the new bridge if it is incomplete.

Local style: the first edit should be syntactically small and proof-local: local ghost/int names, one or a few local `assert(...)` / `assert(...) by { ... }` bridge facts, and no new hard admits. Pull repeated products, powers, casts, and coefficient sums into simple local names before any nonlinear arithmetic. Do not add a new top-level GT-shaped lemma signature as the first move; if a helper is needed, discover that from the first part1 verifier result.

Verifier rhythm: after the first source edit, run only the scoped part1-chain module check. Do not run part2-chain, main Montgomery, backend scalar, package, or whole-crate checks until the part1 edit has landed a real same-module signal. Do not raise rlimit before the first edit. If the part1 check still times out or fails at the new bridge, split that same bridge further; do not switch proof threads.

Progress accounting: a real step is a worktree diff in `lemma_nl_decomposition` followed by a part1-chain check that names or advances that bridge, one bridge fact that verifies under the part1-chain check, or one removed hard admit in the part1-chain file. A lower broad-package error count, a helper-library reading tour before the first diff, or new hard admits elsewhere is not progress. COMPLETE is impossible from this phase alone; this run is meant to force the first decomposition edit so the lane can later climb back through part2/main/scalar integration.
"""
elif thread in ("decomp-helper", "helper-bridge", "micro-bridge"):
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    lane_anchor = lane_part1_chain
    manifest["target"] = lane_anchor
    active_editable = [lane_part1_chain]
    manifest["name"] = manifest["name"] + "::thread-decomposition-helper-extraction"
    thread_brief = """Operator-helper-extraction scheduler for this run. This is a natural-language proof-method packet, not a GT lemma signature list and not a hard-coded proof script.

Firewall label: this diagnostic run tells the agent the next proof-structure move after prior attempts timed out. A success here shows the agent can execute an operator-pinned helper extraction, not that it independently found the field-floor order or decomposition. Do not describe this as a full field-floor result, a ceiling break, or an unguided §11-style result.

Why this packet exists: the prior first-edit run made the requested local bridge edit in `lemma_nl_decomposition`, then expanded that body with many local product and regrouping assertions. The part1-chain module still timed out twice, and the next action became a longer `--timeout 600` verifier run. The old rlimit/timeout path is now known non-progress. The next useful edit is to move one tiny bridge out of the giant body into its own same-file helper proof and call it from `lemma_nl_decomposition`.

Active edit scope: edit only `montgomery_reduce_part1_chain_lemmas.rs` in this run. The rest of the scalar-Montgomery lane is present for provenance, reading after the first edit if needed, and later accounting; it is not writable in this phase. Do not touch `montgomery_reduce_lemmas.rs`, `montgomery_reduce_part2_chain_lemmas.rs`, or `scalar.rs`.

First-action rule: your first tool action may read the active part1-chain file to locate `lemma_nl_decomposition` and match local naming/style. After that, make a source edit before opening helper libraries, running Verus, searching, or inspecting broad package state. The first diff should add one separate same-file helper proof and call it from `lemma_nl_decomposition`; it should not add another large in-body expansion block.

Concrete first helper in natural language: prove only the constant-coefficient distribution piece of the `n * L_value` bridge. In words: multiplying the five-limb base-P value `n` by the lowest coefficient of `L_value` equals the sum of that coefficient's five shifted limb terms at powers P^0 through P^4. Derive the exact helper parameters and contract shape from the current file's locals and style; do not copy a GT signature. The helper should mention only this one distribution fact, not the full low/high split, not all coefficients, and not the final regroup.

After the first helper: repeat the same pattern one bridge at a time. Next candidates are the L1-at-P distribution, then the L2-at-P^2 distribution, then the L4-at-P^4 distribution, then the `nl_high * P5` bridge, then the final regroup. Do not start the next helper until the current helper either verifies or the part1-chain check reports a concrete local span inside it.

GT-style proof method, stated only as style: use small named proof helpers and scoped `assert(...) by { ... }` blocks around individual lemma calls. Search same file first, then common multiplication/power lemmas, then scalar-domain lemmas, but only after the first helper edit is present. Keep nonlinear arithmetic goals at the size of a two-term distribution or power-product fact. A bulk `by (nonlinear_arith)` over the whole 5x5 expansion is the timeout pattern we are avoiding.

Verifier rhythm: after the first helper-and-call edit, run only the scoped part1-chain module check. Do not run part2-chain, main Montgomery, backend scalar, package, or whole-crate checks while this helper is unresolved. Do not raise timeout or rlimit in response to another line-0 timeout; shrink the helper or split it again. Never start a second verifier while an earlier verifier command from this run is still live.

Progress accounting: a real step is one new same-file helper proof that survives the part1-chain module check, one checker error/span that moves from the giant `lemma_nl_decomposition` body into the helper you just wrote, or one removed hard admit in the part1-chain file. A longer verifier timeout, a lower broad-package error count, a helper-library reading tour before the first diff, or new hard admits elsewhere is not progress. COMPLETE is impossible from this phase alone; this run is meant to bank the first helper extraction so the lane can later climb back through part2/main/scalar integration.
"""
elif thread in ("constant-dist", "const-dist", "l0-only", "l0-micro"):
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    lane_anchor = lane_part1_chain
    manifest["target"] = lane_anchor
    active_editable = [lane_part1_chain]
    manifest["name"] = manifest["name"] + "::thread-constant-coefficient-micro-proof"
    thread_brief = """Operator constant-coefficient micro-proof scheduler for this run. This is a natural-language proof-method packet, not a GT lemma signature list, not copied GT proof order, and not a hard-coded proof script.

Firewall label: this diagnostic run is deliberately operator-pinned to one local proof subproblem. A success here means the agent can finish the current tiny helper under a strict schedule; it is not a full field-floor result, not a ceiling break, and not evidence that the agent independently discovered the decomposition order.

Why this packet exists: the previous helper-extraction run made the intended first edit by adding a same-file helper for the constant-coefficient distribution bridge and calling it from the decomposition proof. The first clean verifier result was still a timeout. After that, the run expanded sideways into higher-coefficient and high-product helpers while leaving the constant-coefficient helper body as a broad nonlinear proof, and the timeout did not improve. Another side lane tried rlimit/long-timeout variants and worsened the error surface. Treat those as negative examples. The useful comparison is proof shape: the failing shape asks the solver to discover nonlinear distribution; the verifiable shape hand-feeds an explicit multiplication ladder through scoped assertions.

Known starting signal: you do not need to rediscover that the part1-chain check times out or fails before the helper is split. That fact is the input to this packet. A pre-edit verifier run only repeats the known failure and does not count as diagnosis.

Active edit scope: edit only `montgomery_reduce_part1_chain_lemmas.rs`. Inside that file, stay inside the current constant-coefficient distribution helper body until it verifies or until the checker reports a concrete local assertion span inside that helper. Do not edit the final regroup. Do not add higher-coefficient distribution helpers, high-product helpers, part2 helpers, main Montgomery helpers, scalar.rs proof blocks, or caller-facing helpers in this run.

First-action rule: read the active part1-chain file only enough to locate the current decomposition proof and the current or needed constant-coefficient helper. After that read, the next non-read action must be a source edit that rewrites that helper body into tiny local assertions. Running `verus_check`, cargo-verus, a broad package check, a helper-library search, or a second diagnostic Bash command before this edit is a scheduler failure for this run.

Concrete repair shape in natural language: split the five-term distribution into an explicit multiplication ladder. Start from the first shifted product as a named prefix equality, then extend the prefix by exactly one shifted product per scoped assertion until all five shifted products are present. Normalize one product at a time: name the limb, the coefficient, the power, the product, and the shifted product as simple local ghost/int facts before calling the existing multiplication, power, associativity, or commutativity facts that connect them. Each `assert ... by { ... }` block should contain the few lemma calls needed for that one ladder step, not a solver search over the whole expression. Leave the intermediate prefix facts visible so the checker can point at the exact failed span.

Nonlinear-arithmetic ban for this helper: do not use `by (nonlinear_arith)` inside the constant-coefficient helper, even on a smaller subgoal. Replacing one large nonlinear blast with several smaller nonlinear blasts is still the wrong proof shape for this diagnostic. If a step needs nonlinear reasoning, split it again or search for/call a tiny multiplication or power lemma that states the needed algebraic fact.

Verifier rhythm: after the constant-coefficient helper-body edit, run only the scoped part1-chain module check once. If it times out or reports a line-0 failure, do not raise timeout, do not raise rlimit, do not start a second live verifier, and do not add sibling helpers. Return to that helper body and split the failing prefix, product-normalization, lemma-call, or join assertion further. If the checker gives a concrete assertion span inside the constant-coefficient helper, repair that span before moving anywhere else.

Progress accounting: a real step is a verified or smaller constant-coefficient helper under the same scoped part1-chain check, one explicit ladder assertion span in that helper that replaces the previous whole-helper timeout, or one removed hard admit in the part1-chain file. New sibling helpers, broad package/scalar checks, longer verifier budgets, lower off-thread error counts, smaller `nonlinear_arith` blasts, or new hard admits are not progress. COMPLETE is impossible from this phase alone; this run exists to bank the constant-coefficient micro-proof so a later lane can climb to the next bridge.
"""
elif thread == "product-bound":
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    manifest["name"] = manifest["name"] + "::thread-product-bound"
    thread_brief = """Product-bound proof-thread scheduler for this run: keep the whole scalar-Montgomery lane writable, but the current thread is the product-bound expression-shape repair in `montgomery_reduce_lemmas.rs`, especially `lemma_bounded_product_satisfies_input_bounds` and the nearby `by (bit_vector) requires` blocks that mention `Scalar52` limb/array field accesses.

First action: run the same scoped Montgomery lemma-module check and read only the current source spans. If the check reports Verus internal panics or type/visibility errors in this product-bound area, make the smallest local repair before doing any more broad search. This run should not spend another cycle rediscovering scalar byte-packing specs, GT-style lemma names, or unrelated scalar APIs.

Concrete repair shape, stated in natural language: before a `by (bit_vector)` block, pull every opaque or compound expression out into local names. Extract `a.limbs[i]` and `b.limbs[j]` into simple limb locals, cast them once into simple integer/nat locals, name the products/sums that the bit-vector prover needs, and assert their ordinary bounds outside the bit-vector block. The bit-vector block should see simple locals and casts, not repeated struct-field, array-index, view, or spec-function expressions. Prefer two or three small assertions over one giant rewritten theorem.

Work order: first eliminate the product-bound panics around `lemma_bounded_product_satisfies_input_bounds`. Only after those spans disappear may you touch the byte-packing/as-bytes boundary. Do not copy or expand a broad `lemma_as_bytes_52` rewrite as the first move; that is a later repair only if the product-bound thread is already stable.

Lane permissions: Montgomery lemma files are the main proof surface. `scalar.rs` proof blocks are allowed when a frozen `montgomery_reduce` or `from_montgomery` caller directly demands a lane fact, but they are not the first move for this thread. Do not chase Pippenger/Straus/digit/radix/NAF, Edwards, Ristretto, or generic scalar cleanup unless you can write the direct dependency path back to this product-bound Montgomery proof.

Convergence rule: after each edit, rerun the same scoped Montgomery check. A real step is removal of the product-bound panic/type error with no new hard admits. A lower error count with new admits, broad stubs, or a new off-lane scaffold is not progress. If two consecutive checks show no diff from you, stop searching and perform the local extraction repair described above.

Stop condition: scoped lane green with zero new hard admits is only a lane signal. The operator still owes the de-stubbed whole-crate integration gate before counting this as field-floor progress.
"""
elif thread == "as-bytes":
    if lane_scope != "structural":
        raise SystemExit("--thread requires structural lane scope")
    active_editable = [lane_lemma_anchor]
    manifest["name"] = manifest["name"] + "::thread-as-bytes-boundary"
    thread_brief = """As-bytes boundary proof-thread scheduler for this run: the current proof thread is local to `lemma_as_bytes_52` in `montgomery_reduce_lemmas.rs`. The purpose is to finish the byte-boundary reconstruction exposed by the zero-admit Montgomery frontier, not to rediscover the whole scalar lane.

Starting point: previous zero-admit work already cleared the product-bound expression-shape panics and the `product[k]` extraction problem. Treat that as the baseline. Do not go back to product-bound work unless the current scoped verifier output explicitly points there again.

First action: run exactly one scoped Montgomery lemma-module check and read the exact source spans. If you need to reformat the JSON, print the `summary` and `error_texts` fields; do not loop over `messages[].message`, and do not run the same verifier command repeatedly just to get nicer output. At most one formatting rerun is allowed, and only after the previous verifier process has exited. If the first span is the byte-6 boundary in `lemma_as_bytes_52`, finish that boundary before touching byte 19. If byte 6 is clear and byte 19 is named, repeat the same proof shape there. If the verifier names a cast/mod assertion instead of the small power fact, stay in the same byte-boundary proof and repair that local assertion.

Proof shape, in natural language: avoid `compute_only` and avoid reveal/reveal-with-fuel for opaque powers of two. Use the existing concrete small-power facts from the common power/power2 library, then name the low-byte, high-nibble, shifted-nibble, and casted-byte values as simple locals before proving their nat equality. Keep bit-vector reasoning on simple machine-integer locals; keep spec/nat equalities outside the bit-vector block. Prefer two or three local bridge assertions over a large theorem.

Order rule: byte 6 boundary first, byte 19 boundary second, then rerun the same scoped Montgomery check. Do not widen to `scalar.rs`, a scalar-module check, or a whole-crate check while this local thread still has active scoped errors or a live verifier process. Do not chase Pippenger/Straus/digit/radix/NAF, Edwards, Ristretto, or unrelated scalar API failures in this run.

Allowed edits: edit only `montgomery_reduce_lemmas.rs` for this thread. The sibling lane files and `scalar.rs` are present for reading and accounting, but they are inactive unless an operator relaunches a broader lane packet.

Convergence rule: a real step is one byte-boundary obligation discharged under the same scoped Montgomery check with zero new hard admits. A lower error count with added admits, new stubs, or broader consumer drift is not progress. If the check times out after a small local repair, split the byte-boundary proof into a tiny local helper inside this file and immediately rerun the same scoped check.

Stop condition: scoped Montgomery green with zero new hard admits is a lane signal, not final field-floor completion. The operator still owes a de-stubbed scalar/whole-crate integration gate before counting it as banked field-floor progress.
"""

if active_editable != [entry.get("path") for entry in manifest.get("files", [])]:
    manifest["active_editable_files"] = active_editable

first_thread_hint = (
    "Prefer one Montgomery proof thread at a time. Good early leaves are "
    "product-bound expression-shape repair, carry-shift, part1 correctness, "
    "part2 bounds, and carry8 bound; "
    "part1-chain divisibility is contract repair only if the current "
    "consumer trace demands it."
)
if thread == "chain-first":
    first_thread_hint = (
        "Post-run024 order: clean part1-chain first, then part2-chain, "
        "then main Montgomery lemma-module errors, then scalar.rs caller "
        "proof blocks / backend-scalar consumer diagnostics."
    )
elif thread in ("decomp-control", "decomposition-control"):
    first_thread_hint = (
        "Decomposition-control order: edit lemma_nl_decomposition in "
        "part1-chain first, split the n * L_value polynomial equality into one "
        "named bridge, then rerun only the part1-chain module check."
    )
elif thread in ("decomp-first-edit", "first-bridge"):
    first_thread_hint = (
        "First-edit order: read the part1-chain file, immediately make one "
        "local bridge edit inside lemma_nl_decomposition for the n * L_value "
        "product-expansion / coefficient-collection / P5 split, then rerun "
        "only the part1-chain module check."
    )
elif thread in ("decomp-helper", "helper-bridge", "micro-bridge"):
    first_thread_hint = (
        "Helper-extraction order: read the part1-chain file, immediately add "
        "one same-file helper proof for the constant-coefficient distribution "
        "piece of the "
        "n * L_value bridge and call it from lemma_nl_decomposition, then "
        "rerun only the part1-chain module check. Do not raise timeout or "
        "rlimit; split the helper instead."
    )
elif thread in ("constant-dist", "const-dist", "l0-only", "l0-micro"):
    first_thread_hint = (
        "Constant-coefficient order: read the part1-chain file, rewrite only "
        "the current constant-coefficient distribution helper body into an "
        "explicit multiplication ladder of scoped assert-by lemma calls before "
        "any verifier run, with no by(nonlinear_arith) in that helper. Then run "
        "one scoped part1-chain check. If it times out, stay in that helper and "
        "split again; do not add sibling helpers or raise timeout/rlimit."
    )

manifest["lane"] = {
    "name": "scalar-montgomery",
    "scope": lane_scope,
    "thread": thread or None,
    "structural_edit_scope": lane_scope == "structural",
    "active_editable": active_editable,
    "run_id": str(run_id),
    "anchor": lane_anchor,
    "lemma_anchor": lane_lemma_anchor,
    "editable": lane_editable if lane_scope == "structural" else [entry.get("path") for entry in manifest.get("files", [])],
    "targets": lane_editable,
    "read_anchors": lane_read_anchors,
    "first_thread_hint": first_thread_hint,
    "policy": (
        "Focus on one scalar Montgomery reduction proof thread at a time; "
        "off-lane failures are next-lane debt unless they directly trace to "
        "a current Montgomery contract or proof."
    ),
}

text = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
out_path.write_text(text)
meta_path.write_text(json.dumps(manifest["lane"], indent=2) + "\n")
brief_path.write_text(thread_brief)
PY

printf '%s|proof|2|%s\n' "$lane_manifest" "$MINUTES" > "$manifests_file"

if [ -z "$PROVENANCE" ]; then
    if [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "lane-plan" ] && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/lane-plan+operator-seed: scalar Montgomery lane peeled for oracle hygiene; scalar.rs is the consumer/check anchor while the full four-file lane remains writable; NL scheduler pins one proof thread at a time; scalar API proof blocks are allowed only when they trace to Montgomery reduction; operator-owned off-lane compile-debt/source seed pre-applied and frozen/non-scoreable; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "lane-plan" ]; then
        PROVENANCE="lane-isolation/lane-plan: scalar Montgomery lane peeled for oracle hygiene; scalar.rs is the consumer/check anchor while the full four-file lane remains writable; NL scheduler pins one proof thread at a time; scalar API proof blocks are allowed only when they trace to Montgomery reduction; not a full lane or field-floor ceiling result."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "frontier" ] && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/frontier+operator-seed: scalar Montgomery lane peeled for oracle hygiene; full lane remains writable, NL verifier-frontier scheduler pins one proof thread at a time; operator-owned off-lane compile-debt/source seed pre-applied and frozen/non-scoreable; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "frontier" ]; then
        PROVENANCE="lane-isolation/frontier: scalar Montgomery lane peeled for oracle hygiene; full lane remains writable, NL verifier-frontier scheduler pins one proof thread at a time; not a full lane or field-floor ceiling result."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "chain-first" ] && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/chain-first+operator-seed: scalar Montgomery lane peeled for oracle hygiene; full lane remains writable, NL scheduler pins post-run024 order: part1-chain, part2-chain, main Montgomery, scalar caller blocks; operator-owned source seed pre-applied and frozen/non-scoreable; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "chain-first" ]; then
        PROVENANCE="lane-isolation/chain-first: scalar Montgomery lane peeled for oracle hygiene; full lane remains writable, NL scheduler pins post-run024 order: part1-chain, part2-chain, main Montgomery, scalar caller blocks; not a full lane or field-floor ceiling result."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "decomp-control" ] || [ "$THREAD" = "decomposition-control" ]; } && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/operator-decomposition-guided+operator-seed: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator prescribes the lemma_nl_decomposition split and requires a bridge edit before any verifier/rlimit cycling; operator-owned source seed pre-applied and frozen/non-scoreable; not an unguided ceiling result; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "decomp-control" ] || [ "$THREAD" = "decomposition-control" ]; }; then
        PROVENANCE="lane-isolation/operator-decomposition-guided: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator prescribes the lemma_nl_decomposition split and requires a bridge edit before any verifier/rlimit cycling; not an unguided ceiling result and not a full field-floor result."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "decomp-first-edit" ] || [ "$THREAD" = "first-bridge" ]; } && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/operator-first-edit-guided+operator-seed: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator prescribes the first lemma_nl_decomposition bridge edit before helper search/verifier/rlimit cycling; operator-owned source seed pre-applied and frozen/non-scoreable; not an unguided ceiling result; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "decomp-first-edit" ] || [ "$THREAD" = "first-bridge" ]; }; then
        PROVENANCE="lane-isolation/operator-first-edit-guided: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator prescribes the first lemma_nl_decomposition bridge edit before helper search/verifier/rlimit cycling; not an unguided ceiling result and not a full field-floor result."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "decomp-helper" ] || [ "$THREAD" = "helper-bridge" ] || [ "$THREAD" = "micro-bridge" ]; } && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/operator-helper-extraction+operator-seed: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator prescribes one same-file helper proof for the lemma_nl_decomposition constant-coefficient distribution bridge before helper search/verifier/rlimit cycling; operator-owned source seed pre-applied and frozen/non-scoreable; not an unguided ceiling result; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "decomp-helper" ] || [ "$THREAD" = "helper-bridge" ] || [ "$THREAD" = "micro-bridge" ]; }; then
        PROVENANCE="lane-isolation/operator-helper-extraction: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator prescribes one same-file helper proof for the lemma_nl_decomposition constant-coefficient distribution bridge before helper search/verifier/rlimit cycling; not an unguided ceiling result and not a full field-floor result."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "constant-dist" ] || [ "$THREAD" = "const-dist" ] || [ "$THREAD" = "l0-only" ] || [ "$THREAD" = "l0-micro" ]; } && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/operator-constant-distribution+operator-seed: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator constrains next source edits to the constant-coefficient distribution helper body as an explicit scoped multiplication ladder, no GT literals, no nonlinear_arith blasts, no sibling-helper fanout, and no verifier-budget cycling; operator-owned source seed pre-applied and frozen/non-scoreable; not an unguided ceiling result; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && { [ "$THREAD" = "constant-dist" ] || [ "$THREAD" = "const-dist" ] || [ "$THREAD" = "l0-only" ] || [ "$THREAD" = "l0-micro" ]; }; then
        PROVENANCE="lane-isolation/operator-constant-distribution: scalar Montgomery lane peeled for oracle hygiene; active edit scope is part1-chain only; operator constrains next source edits to the constant-coefficient distribution helper body as an explicit scoped multiplication ladder, no GT literals, no nonlinear_arith blasts, no sibling-helper fanout, and no verifier-budget cycling; not an unguided ceiling result and not a full field-floor result."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "product-bound" ] && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/product-bound+operator-seed: scalar Montgomery lane peeled for oracle hygiene; full lane remains writable, NL scheduler pins the product-bound expression-shape proof thread first; operator-owned off-lane compile-debt/source seed pre-applied and frozen/non-scoreable; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "product-bound" ]; then
        PROVENANCE="lane-isolation/product-bound: scalar Montgomery lane peeled for oracle hygiene; full lane remains writable, NL scheduler pins the product-bound expression-shape proof thread first; not a full lane or field-floor ceiling result."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "as-bytes" ] && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/as-bytes+operator-seed: scalar Montgomery lane peeled for oracle hygiene; active edit scope is the main Montgomery lemma file; NL scheduler pins lemma_as_bytes_52 byte-boundary repair first; operator-owned off-lane compile-debt/source seed pre-applied and frozen/non-scoreable; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && [ "$THREAD" = "as-bytes" ]; then
        PROVENANCE="lane-isolation/as-bytes: scalar Montgomery lane peeled for oracle hygiene; active edit scope is the main Montgomery lemma file; NL scheduler pins lemma_as_bytes_52 byte-boundary repair first; not a full lane or field-floor ceiling result."
    elif [ "$LANE_SCOPE" = "structural" ] && [ -n "$THREAD" ] && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/thread+operator-seed: scalar Montgomery lane peeled for oracle hygiene; active proof-thread '$THREAD' edit scope only; operator-owned off-lane compile-debt/source seed pre-applied and frozen/non-scoreable; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ] && [ -n "$THREAD" ]; then
        PROVENANCE="lane-isolation/thread: scalar Montgomery lane peeled for oracle hygiene; active proof-thread '$THREAD' edit scope only; not a full lane or field-floor ceiling result."
    elif [ "$LANE_SCOPE" = "structural" ] && [ -n "$OPERATOR_SEED" ]; then
        PROVENANCE="lane-isolation/structural+operator-seed: scalar Montgomery editable scope only; operator-owned off-lane compile-debt/source seed pre-applied and frozen/non-scoreable; de-stubbed integration still required."
    elif [ "$LANE_SCOPE" = "structural" ]; then
        PROVENANCE="lane-isolation/structural: scalar Montgomery editable scope only (montgomery_reduce* lemmas + scalar.rs); off-lane field-floor cone frozen at ref; not a full field-floor ceiling result."
    elif [ -n "$SEED_WIP" ]; then
        PROVENANCE="lane-filter/prompt-only-with-seed: full field-floor editable cone plus scalar Montgomery prompt metadata; not lane-isolated or scoreable."
    else
        PROVENANCE="lane-filter/prompt-only: full field-floor editable cone plus scalar Montgomery prompt metadata; not lane-isolated or scoreable."
    fi
fi

cmd=( "$here/run_agents.sh"
      --image "$IMAGE"
      --gitroot "$GITROOT"
      --ref "$REF"
      --run-id "$RUN_ID"
      --manifests-file "$manifests_file"
      --work-base "$WORK_BASE"
      --rounds "$ROUNDS"
      --max-parallel "$MAX_PARALLEL"
      --cargo-jobs "$CARGO_JOBS" )

[ -z "$SEED_WIP" ] || cmd+=( --seed-wip "$SEED_WIP" )
[ -z "$FAILURE_MEMORY_SEED" ] || cmd+=( --failure-memory-seed "$FAILURE_MEMORY_SEED" )
[ -z "$OPERATOR_SEED" ] || cmd+=( --operator-seed "$OPERATOR_SEED" )
if [ "$REQUIRE_TAP" = "1" ]; then
    cmd+=( --require-tap )
else
    cmd+=( --tap )
fi

echo "lane manifest: $lane_manifest"
echo "lane metadata:  $lane_meta"
echo "manifest list:  $manifests_file"
echo "lane scope:     $LANE_SCOPE"
[ -z "$THREAD" ] || echo "thread:         $THREAD"
[ -z "$OPERATOR_SEED" ] || echo "operator seed: $OPERATOR_SEED"
[ ! -s "$brief_file" ] || echo "thread brief:   $brief_file"
echo "provenance:     $PROVENANCE"
printf 'command:'
printf ' %q' "${cmd[@]}"
printf '\n'

if [ "$DRY_RUN" = "1" ]; then
    exit 0
fi

export DALEK_EXPERIMENT_PROVENANCE="$PROVENANCE"
[ ! -s "$brief_file" ] || export DALEK_EXPERIMENT_BRIEF="$(cat "$brief_file")"
if [ "$PRE_EDIT_DIAGNOSTIC_BLOCK" = "1" ]; then
    export DALEK_PRE_EDIT_DIAGNOSTIC_BLOCK=1
else
    unset DALEK_PRE_EDIT_DIAGNOSTIC_BLOCK
fi
exec "${cmd[@]}"
