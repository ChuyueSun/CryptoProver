#!/usr/bin/env bash
#
# Package and optionally launch the field-floor cut as four lane-isolated
# manifests. This is operator-side orchestration only: run.py still receives one
# manifest/target at a time from docker/run_agents.sh.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/.." && pwd)

BASE_MANIFEST="$repo/peel_manifests/field_floor.json"
IMAGE="${DALEK_LANE_IMAGE:-dalek-harness:f3bfa28-fix11-activeguard-thread}"
GITROOT=""
REF=""
RUN_ID=""
WORK_BASE="${DALEK_LANE_WORK_BASE:-/srv/agents}"
SEED_WIP=""
FAILURE_MEMORY_SEED=""
OPERATOR_SEED=""
ROUNDS=16
MINUTES=360
CARGO_JOBS=4
MAX_PARALLEL=4
REQUIRE_TAP=1
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage:
  docker/package_field_floor_4lanes.sh --run-id ID --gitroot REPO --ref REF [options]

Creates:
  <work-base>/<run-id>/_launcher/field_floor_4lanes_*.json
  <work-base>/<run-id>/_launcher/manifests.txt
  <work-base>/<run-id>/_launcher/lane_plan.md
  <work-base>/<run-id>/_launcher/lane_summary.json

Options:
  --base-manifest PATH   Base field-floor peel manifest
  --image IMAGE          Docker image to run
  --work-base DIR        Host work/results base passed to run_agents.sh
  --seed-wip PATCH       Optional common guarded WIP resume patch
  --failure-memory-seed JSON
                         Prior failure_memory.json copied into each isolated
                         agent /results before prompt render
  --operator-seed PATCH  Operator-owned source seed applied post-peel/pre-seal
  --rounds N             run.py rounds (default: 16)
  --minutes N            per-round task minutes in the manifest list (default: 360)
  --cargo-jobs N         CARGO_BUILD_JOBS per container (default: 4)
  --max-parallel N       Container concurrency (default: 4)
  --no-require-tap       Use best-effort --tap instead of fail-closed --require-tap
  --dry-run              Write generated files and print the command, do not launch

Environment forwarded to run_agents.sh when set:
  CLAUDE_CODE_OAUTH_TOKEN, DALEK_TAP_BASE_PORT, DALEK_TAP_LIVE_BASE,
  DALEK_TAP_OUT, DALEK_TAP_LOG, DALEK_VSTD_CONTAINER
EOF
}

die() { echo "package_field_floor_4lanes: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
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
        --no-require-tap) REQUIRE_TAP=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done

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

manifests_file="$launcher_dir/manifests.txt"
summary_file="$launcher_dir/lane_summary.json"
plan_file="$launcher_dir/lane_plan.md"

python3 - "$BASE_MANIFEST" "$launcher_dir" "$manifests_file" "$summary_file" "$plan_file" "$RUN_ID" "$MINUTES" <<'PY'
import json
import sys
from pathlib import Path

base_path = Path(sys.argv[1])
launcher_dir = Path(sys.argv[2])
manifests_file = Path(sys.argv[3])
summary_file = Path(sys.argv[4])
plan_file = Path(sys.argv[5])
run_id = sys.argv[6]
minutes = sys.argv[7]

base = json.loads(base_path.read_text())
entries = {entry["path"]: entry for entry in base["files"]}

def lane(name, target, files, order, brief, read_anchors=None, policy=None):
    override = (
        "This operator lane brief overrides any earlier generic current-lane "
        "wording in the base prompt, including scalar-Montgomery wording from "
        "older field-floor packets. Follow this lane's anchor, editable files, "
        "and order.\n\n"
    )
    return {
        "name": name,
        "target": target,
        "files": files,
        "order": order,
        "brief": override + brief.strip() + "\n",
        "read_anchors": read_anchors or [],
        "policy": policy or (
            "Work one proof thread at a time. Treat off-lane failures as "
            "integration debt unless they trace directly to this lane."
        ),
    }

lanes = [
    lane(
        "scalar-montgomery",
        "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_lemmas.rs",
        [
            "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_part1_chain_lemmas.rs",
            "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_part2_chain_lemmas.rs",
            "curve25519-dalek/src/lemmas/scalar_lemmas_/montgomery_reduce_lemmas.rs",
            "curve25519-dalek/src/scalar.rs",
        ],
        [
            "Bank part1-chain and part2-chain leaves first.",
            "Then repair main Montgomery composition bounds.",
            "Treat the byte-packing/as_bytes proof as a separate local thread.",
            "Only then repair scalar.rs Montgomery/from-Montgomery proof blocks.",
        ],
        """
Four-lane package, lane 1: scalar Montgomery reduction. The target file is only
the harness anchor. Your editable unit is the Montgomery reduction lane:
part1-chain, part2-chain, main Montgomery reduction lemmas, and lane-relevant
proof blocks in scalar.rs.

Convergence order: bank one proof thread at a time. First stabilize the leaf
chains; then the small main-file leaf bounds; then the heavy composition bounds;
then the byte-packing boundary as its own thread; then scalar.rs caller proof
blocks. If a verifier result points at scalar.rs, trace the dependency back to
Montgomery reduction before editing. Scalar API proof blocks are allowed when
they discharge montgomery_reduce/from_montgomery obligations, but arbitrary
scalar cleanup is not.

Proof style: do not blast large limb products with one nonlinear_arith block.
Split products, powers, casts, and byte boundaries into tiny named facts with
scoped assert-by blocks. If a thread times out, shrink the thread; do not raise
budget first and do not start a second proof family.
""",
        read_anchors=[
            "curve25519-dalek/src/backend/serial/u64/scalar.rs",
            "curve25519-dalek/src/specs/montgomery_reduce_specs.rs",
            "curve25519-dalek/src/specs/scalar52_specs.rs",
        ],
    ),
    lane(
        "scalar-digits-bytes",
        "curve25519-dalek/src/lemmas/scalar_lemmas_/radix_2w_lemmas.rs",
        [
            "curve25519-dalek/src/lemmas/scalar_lemmas_/radix_2w_lemmas.rs",
            "curve25519-dalek/src/lemmas/scalar_lemmas_/radix16_lemmas.rs",
            "curve25519-dalek/src/lemmas/scalar_lemmas_/naf_lemmas.rs",
            "curve25519-dalek/src/lemmas/scalar_byte_lemmas/bytes_to_scalar_lemmas.rs",
            "curve25519-dalek/src/lemmas/scalar_byte_lemmas/scalar_to_bytes_lemmas.rs",
        ],
        [
            "radix_2w first.",
            "radix16 after radix_2w.",
            "naf after radix_2w.",
            "byte conversion leaves can run after their direct scalar facts are stable.",
        ],
        """
Four-lane package, lane 2: scalar digit and byte leaves. This lane is
curve-independent. Its job is to reconstruct digit/radix/NAF and scalar-byte
conversion lemmas, not Montgomery reduction, Edwards, Ristretto, or scalar API
cleanup.

Order: start with radix_2w because it feeds the other scalar-digit consumers.
Then do radix16 and NAF. Treat bytes_to_scalar and scalar_to_bytes as independent
leaf threads once their immediate scalar facts are present. For each file, derive
contracts from current frozen callsites and sibling uses; finish one leaf before
opening another.

If broad checks report scalar.rs failures, use them only as caller signal for a
too-weak lemma contract in this lane. Do not edit scalar.rs in this lane package.
Record caller proof-block debt for the integration pass unless the operator
relaunches with scalar.rs explicitly editable.
""",
        read_anchors=[
            "curve25519-dalek/src/scalar.rs",
            "curve25519-dalek/src/specs/scalar52_specs.rs",
        ],
    ),
    lane(
        "edwards-core",
        "curve25519-dalek/src/lemmas/edwards_lemmas/curve_equation_lemmas.rs",
        [
            "curve25519-dalek/src/lemmas/edwards_lemmas/constants_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/curve_equation_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/double_correctness.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/niels_addition_correctness.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/torsion_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/step1_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/decompress_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/mul_base_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/straus_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/pippenger_lemmas.rs",
            "curve25519-dalek/src/lemmas/edwards_lemmas/vartime_double_base_lemmas.rs",
            "curve25519-dalek/src/edwards.rs",
            "curve25519-dalek/src/montgomery.rs",
        ],
        [
            "constants before curve-equation facts.",
            "curve-equation keystone before operation/decode helpers.",
            "operation/decode helpers before scalar-mul algorithms.",
            "mul_base and straus before pippenger; pippenger is not the first task.",
        ],
        """
Four-lane package, lane 3: Edwards core and scalar-mul algorithms. The old broad
field-floor runs got stuck by wandering into Pippenger before the curve-equation
substrate was banked. Do not do that here.

Order: first constants, then the curve-equation keystone facts. After that,
bank operation/decode helpers: double, Niels addition, torsion, step1, and
decompress. Only then work scalar-mul algorithms: mul_base and Straus before
Pippenger, with vartime-double-base last. If Pippenger appears early in a broad
check, classify it as downstream unless its direct prerequisites in this lane
already verify.

Proof style: use the codebase's opaque-point patterns and type-invariant helpers
instead of inventing field access. For curve equations, split affine/projective
bridges, denominator facts, add/double algebra, and scalar-mul recursion into
small proof threads. Keep edwards.rs and montgomery.rs edits to proof blocks and
local assertions that consume the reconstructed Edwards contracts.
""",
        read_anchors=[
            "curve25519-dalek/src/specs/edwards_specs.rs",
            "curve25519-dalek/src/specs/montgomery_specs.rs",
            "curve25519-dalek/src/traits.rs",
        ],
    ),
    lane(
        "ristretto-final",
        "curve25519-dalek/src/ristretto.rs",
        [
            "curve25519-dalek/src/lemmas/ristretto_lemmas/batch_compress_lemmas.rs",
            "curve25519-dalek/src/lemmas/ristretto_lemmas/coset_lemmas.rs",
            "curve25519-dalek/src/lemmas/ristretto_lemmas/elligator_lemmas.rs",
            "curve25519-dalek/src/ristretto.rs",
        ],
        [
            "helper lemmas before ristretto.rs caller blocks.",
            "batch-compress after Niels/curve-equation dependencies are available.",
            "coset after torsion/curve-equation dependencies are available.",
            "elligator can proceed as its own local helper thread.",
        ],
        """
Four-lane package, lane 4: Ristretto helpers and final Ristretto proof surface.
This lane should not reconstruct Edwards or scalar proofs. If a Ristretto proof
is blocked by an upstream missing Edwards/scalar fact, record the exact required
fact as upstream lane debt and continue with a local Ristretto thread whose
dependencies are present.

Order: helper lemmas first, then ristretto.rs proof blocks. Work batch-compress
only after its Niels/curve-equation dependencies are usable; work coset only
after torsion/curve-equation dependencies are usable; elligator can be an
independent helper thread. In ristretto.rs, repair one proof block at a time and
avoid broad refactors of verified helpers.

Proof style: use small local bridge lemmas for byte/equality/affine facts and
call the reconstructed helper lemmas from ristretto.rs. A module-local green is
only a lane signal; the package still needs a final de-stubbed whole-crate
integration pass with zero non-axiom admits.
""",
        read_anchors=[
            "curve25519-dalek/src/specs/ristretto_specs.rs",
            "curve25519-dalek/src/specs/edwards_specs.rs",
        ],
    ),
]

all_lane_paths = [p for item in lanes for p in item["files"]]
base_paths = set(entries)
missing = sorted(set(all_lane_paths) - base_paths)
if missing:
    raise SystemExit(f"lane split references files not in base manifest: {missing}")
duplicates = sorted({p for p in all_lane_paths if all_lane_paths.count(p) > 1})
if duplicates:
    raise SystemExit(f"lane split is not disjoint: {duplicates}")
uncovered = sorted(base_paths - set(all_lane_paths))
if uncovered:
    raise SystemExit(f"lane split does not cover base manifest files: {uncovered}")

manifest_lines = []
summary = {
    "run_id": run_id,
    "base_manifest": str(base_path),
    "package": "field-floor-4lanes",
    "lanes": [],
}

for idx, item in enumerate(lanes, start=1):
    manifest = dict(base)
    manifest["name"] = f"{base.get('name', 'field-floor-cut')}::4lane::{idx}-{item['name']}"
    manifest["target"] = item["target"]
    # Peel the entire field-floor cone in every lane for oracle hygiene. The
    # lane split below controls the active edit scope passed to run.py.
    manifest["files"] = base["files"]
    manifest["active_editable_files"] = item["files"]
    manifest["lane"] = {
        "package": "field-floor-4lanes",
        "index": idx,
        "count": len(lanes),
        "name": item["name"],
        "run_id": run_id,
        "anchor": item["target"],
        "peel_scope": "full-field-floor",
        "editable": item["files"],
        "active_editable": item["files"],
        "targets": item["files"],
        "read_anchors": item["read_anchors"],
        "order": item["order"],
        "policy": item["policy"],
        "operator_brief": item["brief"],
        "provenance": (
            f"field-floor/4lane:{item['name']} lane package; "
            "operator-side manifest split with NL lane order; de-stubbed "
            "whole-crate integration gate required before scoring."
        ),
    }
    out = launcher_dir / f"field_floor_4lanes_{idx}_{item['name']}.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest_lines.append(f"{out}|proof|2|{minutes}")
    summary["lanes"].append({
        "index": idx,
        "name": item["name"],
        "target": item["target"],
        "manifest": str(out),
        "files": item["files"],
        "order": item["order"],
    })

manifests_file.write_text("\n".join(manifest_lines) + "\n")
summary_file.write_text(json.dumps(summary, indent=2) + "\n")

plan = [
    "# Field-floor four-lane package",
    "",
    f"Run id: `{run_id}`",
    f"Base manifest: `{base_path}`",
    "",
    "This package assigns every file in the base field-floor manifest to exactly one",
    "active lane. Each task still peels the full field-floor manifest for oracle",
    "hygiene, then uses `active_editable_files` to restrict the current lane.",
    "The final result still requires a de-stubbed whole-crate integration gate.",
    "",
]
for item in summary["lanes"]:
    plan.append(f"## Lane {item['index']}: {item['name']}")
    plan.append(f"- Anchor: `{item['target']}`")
    plan.append("- Order:")
    for step in item["order"]:
        plan.append(f"  - {step}")
    plan.append("- Editable files:")
    for path in item["files"]:
        plan.append(f"  - `{path}`")
    plan.append("")
plan_file.write_text("\n".join(plan))
PY

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

echo "four-lane package: $launcher_dir"
echo "manifest list:     $manifests_file"
echo "lane summary:      $summary_file"
echo "lane plan:         $plan_file"
[ -z "$OPERATOR_SEED" ] || echo "operator seed:     $OPERATOR_SEED"
[ -z "$FAILURE_MEMORY_SEED" ] || echo "failure memory:    $FAILURE_MEMORY_SEED"
printf 'command:'
printf ' %q' "${cmd[@]}"
printf '\n'

if [ "$DRY_RUN" = "1" ]; then
    exit 0
fi

exec "${cmd[@]}"
