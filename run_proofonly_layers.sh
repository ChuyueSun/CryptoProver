#!/usr/bin/env bash
# run_proofonly_layers.sh — run the proof-only experiment across one or more
# layer sets, via launch.sh.
#
# What it does:
#   1. Resolves each layer set (A/B/C/D or L0..L9 or ALL) to its .rs files,
#      reusing run_layer.py's LAYER_SETS + module_to_file mapping.
#   2. Writes a single launch.sh targets file covering all requested layers.
#   3. Invokes launch.sh with --experiment-mode proof-only.
#
# Sequential by design: one targets file → launch.sh runs every target
# back-to-back against the one project worktree. Do NOT run several copies in
# parallel against the same --project (cargo-lock + failure_memory.json races).
#
# ⚠️  PREREQUISITE: the files under <project>/src/ must already be the STRIPPED
#     proof-only inputs (specs intact, proof bodies broken / seeded with
#     admit()). launch.sh runs the agent on the files as-is — if they still hold
#     full proof bodies, every target verifies instantly and proves nothing.
#
# Usage:
#   ./run_proofonly_layers.sh --project <cargo-root> [--vstd-root <path>] \
#       [--layers "A B C D"] [--run-id <id>] [--results <dir>] \
#       [--rounds N] [--budget-min N] [--model <name>] [--no-detach] [--dry-run]
#
# Examples:
#   ./run_proofonly_layers.sh --project /path/to/curve25519-dalek \
#       --vstd-root /path/to/vstd
#
#   ./run_proofonly_layers.sh --project /path/to/curve25519-dalek \
#       --layers "A B" --run-id pa_001 --dry-run

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Defaults ─────────────────────────────────────────────────────────────────
PROJECT=""
VSTD_ROOT=""
LAYERS="A B C D"
RUN_ID=""
RESULTS="results-proofonly"
ROUNDS="5"
BUDGET_MIN=""
MODEL=""
DETACH="1"          # detached by default (safe from Claude Code's Bash tool)
DRY_RUN="0"
SKIP_EXISTING="0"   # resume: skip targets already in proven_registry.json

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

# ── Parse args ───────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)      usage; exit 0 ;;
    --project)      PROJECT="$2"; shift 2 ;;
    --vstd-root)    VSTD_ROOT="$2"; shift 2 ;;
    --layers)       LAYERS="$2"; shift 2 ;;
    --run-id)       RUN_ID="$2"; shift 2 ;;
    --results)      RESULTS="$2"; shift 2 ;;
    --rounds)       ROUNDS="$2"; shift 2 ;;
    --budget-min)   BUDGET_MIN="$2"; shift 2 ;;
    --model)        MODEL="$2"; shift 2 ;;
    --no-detach)    DETACH="0"; shift ;;
    --skip-existing) SKIP_EXISTING="1"; shift ;;
    --dry-run)      DRY_RUN="1"; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  echo "unexpected arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$PROJECT" ] || { echo "error: --project required" >&2; exit 2; }
[ -d "$PROJECT" ] || { echo "error: --project not a directory: $PROJECT" >&2; exit 2; }
[ -x "$SCRIPT_DIR/launch.sh" ] || { echo "error: launch.sh not found/executable next to this script" >&2; exit 3; }

# Default run-id from the requested layers, e.g. "A B C D" → proofonly_ABCD
if [ -z "$RUN_ID" ]; then
  tag="$(echo "$LAYERS" | tr -d '[:space:]')"
  RUN_ID="proofonly_${tag}"
fi

TARGETS_FILE="/tmp/targets_${RUN_ID}"

# ── 1. Resolve layers → targets file (reuse run_layer.py's mapping) ──────────
PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" - "$PROJECT" "$RESULTS" $LAYERS > "$TARGETS_FILE" 2> /tmp/targets_${RUN_ID}.err <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from run_layer import LAYER_SETS, module_to_file
project, results = Path(sys.argv[1]), sys.argv[2]
unknown = [L for L in sys.argv[3:] if L not in LAYER_SETS]
if unknown:
    print(f"unknown layer set(s): {unknown}; known: {sorted(LAYER_SETS)}", file=sys.stderr)
    sys.exit(2)
n = 0
for layer in sys.argv[3:]:
    print(f"# ---- layer {layer} ----")
    for m in LAYER_SETS[layer]:
        try:
            f = module_to_file(m, project)
            print(f"{results} | {f.relative_to(project)}")
            n += 1
        except FileNotFoundError:
            print(f"# MISSING {layer}: {m}", file=sys.stderr)
print(f"resolved {n} target file(s)", file=sys.stderr)
PY
then
  echo "error: failed to resolve layers (run this from the repo root):" >&2
  cat /tmp/targets_${RUN_ID}.err >&2
  exit 3
fi

echo "[proofonly] layers     = $LAYERS"
echo "[proofonly] run-id     = $RUN_ID"
echo "[proofonly] results    = $RESULTS"
echo "[proofonly] targets    = $TARGETS_FILE"
cat /tmp/targets_${RUN_ID}.err
echo "[proofonly] --- targets file ---"
cat "$TARGETS_FILE"
echo "[proofonly] -------------------"

if [ "$DRY_RUN" = "1" ]; then
  echo "[proofonly] --dry-run: not launching. Remove --dry-run to run."
  exit 0
fi

# ── 2. Launch proof-only across all resolved targets ─────────────────────────
LAUNCH_ARGS=(
  --run-id          "$RUN_ID"
  --project         "$PROJECT"
  --experiment-mode proof-only
  --rounds          "$ROUNDS"
  --targets-file    "$TARGETS_FILE"
)
[ -n "$VSTD_ROOT" ]  && LAUNCH_ARGS+=(--vstd-root  "$VSTD_ROOT")
[ -n "$BUDGET_MIN" ] && LAUNCH_ARGS+=(--budget-min "$BUDGET_MIN")
[ -n "$MODEL" ]        && LAUNCH_ARGS+=(--model      "$MODEL")
[ "$SKIP_EXISTING" = "1" ] && LAUNCH_ARGS+=(--skip-existing)
[ "$DETACH" = "1" ]    && LAUNCH_ARGS+=(--detach)

echo "[proofonly] launching: launch.sh ${LAUNCH_ARGS[*]}"
exec "$SCRIPT_DIR/launch.sh" "${LAUNCH_ARGS[@]}"
