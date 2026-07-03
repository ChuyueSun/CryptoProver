#!/usr/bin/env bash
# launch_spec_proof.sh — launch ONE spec-proof experiment-mode run of run.py.
#
# `launch.sh` drives the normal admit-filling flow but does NOT forward the
# experiment-mode flags (--experiment-mode / --experiment-allow-edit /
# --no-spec-gate), so spec-proof runs were previously invoked by typing the
# full run.py argv by hand. This wraps that argv and adds the same detached
# re-exec that launch.sh uses, so a run launched from inside Claude Code's
# Bash tool survives the tool's process-group teardown.
#
# Defaults target the prepared stripped worktree at
# /private/tmp/dalek-spec-strip (anchor = edwards.rs, 3 stripped deps) — the
# same surface the spec_proof_exp* runs used. Override any of it via flags.
#
# Usage:
#   ./launch_spec_proof.sh [--run-id ID] [--rounds N] [--budget MIN]
#                          [--model M] [--anchor FILE] [--project DIR]
#                          [--vstd DIR] [--dep FILE ...] [--detach]
#
#   # quick smoke run (short budget, watch for Task subagent delegation):
#   ./launch_spec_proof.sh --run-id subagent_check --rounds 2 --budget 15 --detach
#   tail -f launcher_subagent_check.log
set -euo pipefail

MVP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── defaults: the prepared dalek-spec-strip edwards surface ──────────────────
PROJECT="/private/tmp/dalek-spec-strip/curve25519-dalek"
ANCHOR=""
VSTD="/path/to/verus/vstd"
RUN_ID="spec_proof_$(date +%Y%m%d_%H%M%S)"
ROUNDS=2
BUDGET=15
MODEL=""
DETACH=0
DEPS=()

usage() { sed -n '2,30p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)   RUN_ID="$2";  shift 2 ;;
    --rounds)   ROUNDS="$2";  shift 2 ;;
    --budget)   BUDGET="$2";  shift 2 ;;
    --model)    MODEL="$2";   shift 2 ;;
    --anchor)   ANCHOR="$2";  shift 2 ;;
    --project)  PROJECT="$2"; shift 2 ;;
    --vstd)     VSTD="$2";    shift 2 ;;
    --dep)      DEPS+=("$2"); shift 2 ;;
    --detach)   DETACH=1;     shift ;;
    -h|--help)  usage 0 ;;
    *) echo "unknown flag: $1" >&2; usage 2 ;;
  esac
done

# Fill anchor + deps from the project root if not explicitly overridden.
[ -n "$ANCHOR" ] || ANCHOR="$PROJECT/src/edwards.rs"
if [ "${#DEPS[@]}" -eq 0 ]; then
  DEPS=(
    "$PROJECT/src/backend/mod.rs"
    "$PROJECT/src/backend/serial/scalar_mul/vartime_double_base.rs"
    "$PROJECT/src/lemmas/edwards_lemmas/vartime_double_base_lemmas.rs"
  )
fi

# ── preflight ────────────────────────────────────────────────────────────────
[ -d "$PROJECT" ] || { echo "error: --project not a dir: $PROJECT" >&2; exit 2; }
[ -f "$ANCHOR" ]  || { echo "error: --anchor not a file: $ANCHOR" >&2; exit 2; }
for d in "${DEPS[@]}"; do
  [ -f "$d" ] || { echo "error: --dep not a file: $d" >&2; exit 2; }
done

LOG="$MVP_ROOT/launcher_${RUN_ID}.log"

# ── build run.py argv ────────────────────────────────────────────────────────
CMD=(
  python3 "$MVP_ROOT/run.py" "$ANCHOR"
  --project "$PROJECT"
  --run-id  "$RUN_ID"
  --rounds  "$ROUNDS"
  --max-task-minutes "$BUDGET"
  --experiment-mode spec-proof
  --no-spec-gate
  --experiment-allow-edit "${DEPS[@]}"
)
[ -n "$VSTD" ]  && CMD+=(--vstd-root "$VSTD")
[ -n "$MODEL" ] && CMD+=(--model "$MODEL")

echo "spec-proof launch:"
echo "  anchor : $ANCHOR"
echo "  project: $PROJECT"
echo "  deps   : ${#DEPS[@]} stripped file(s)"
echo "  run-id : $RUN_ID   rounds=$ROUNDS budget=${BUDGET}min model=${MODEL:-default}"
echo "  log    : $LOG"

# ── detach: re-exec via Python start_new_session (POSIX setsid) ──────────────
# macOS has no `setsid` binary; Python (3.11+, already required) gives us a
# fresh session/process-group so the run survives Claude Code's Bash-tool
# killpg on teardown. Same mechanism as launch.sh --detach.
if [ "$DETACH" = "1" ]; then
  export _SP_LOG="$LOG"
  PID=$(python3 - "${CMD[@]}" <<'PYEOF'
import os, subprocess, sys
log_path = os.environ['_SP_LOG']
p = subprocess.Popen(
    sys.argv[1:],
    stdin=subprocess.DEVNULL,
    stdout=open(log_path, 'w'),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(p.pid)
PYEOF
)
  echo "$PID" > "${LOG%.log}.pid"
  echo "launched detached pid=$PID  pid_file=${LOG%.log}.pid"
  echo "watch:  tail -f $LOG"
  exit 0
fi

# Foreground.
exec "${CMD[@]}" 2>&1 | tee "$LOG"
