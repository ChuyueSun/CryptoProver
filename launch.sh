#!/usr/bin/env bash
# launch.sh — sequential multi-target launcher for run.py.
#
# Why this exists: run.py is one-target-per-invocation by design. Most
# real workflows run several targets back-to-back against the same
# project worktree. Doing that as a hand-rolled bash loop is fine until
# you need to background it from Claude Code's Bash tool, at which point
# `nohup … & disown` quietly fails — the tool teardown does a `killpg`
# on its child process group, and `nohup` only protects against SIGHUP.
# This launcher handles the only-tricky-bit (full session detach via
# POSIX setsid) so callers don't reinvent it.
#
# Clean admitted worktree (the starting state each run wants): a target
# in its admitted state (proof bodies -> admit()) inside an isolated
# checkout. dalek-lite is a Cargo WORKSPACE — the worktree is added at the
# git repo root; pass the curve25519-dalek/ member subdir as --project.
# Use admit.py's create_admit_worktree (the in-repo counterpart of
# inference-dalek's construct_admitted_state + StartingStateManager.checkout):
#   REPO=/path/to/dalek-lite            # project git repo root (workspace)
#   # A: pre-built admitted ref (skeleton already committed):
#   python admit.py --worktree /tmp/wt --gitroot "$REPO" --ref eval/admitted-start
#   ./launch.sh --run-id w1 --project /tmp/wt/curve25519-dalek src/edwards.rs
#   # B: build skeleton from clean source (--admit-target admits in place):
#   python admit.py --worktree /tmp/wt --gitroot "$REPO" --ref main \
#       --admit-target curve25519-dalek/src/edwards.rs
#   ./launch.sh --run-id w1 --project /tmp/wt/curve25519-dalek src/edwards.rs
#   python admit.py --worktree /tmp/wt --gitroot "$REPO" --remove   # cleanup
# (Or by hand: git -C "$REPO" worktree add --detach /tmp/wt <ref>, then
#  launch.sh --admit. `git worktree add <wt> main` without --detach fails —
#  the primary checkout already holds main.)
#
# Sequential — never parallel against the SAME project (cargo-lock
# contention + failure_memory.json read-modify-write races). To fan out,
# give each worker its OWN worktree AND its own --results dir, one
# launch.sh per worktree. Sharing either reintroduces a race.
#
# Two ways to specify targets:
#   1. Positional args: paths to .rs files (all share --results, etc.)
#   2. --targets-file: one line per target, supports per-target overrides.
#      Format: <results-dir>|<rel-path>[|<budget-min>]   # comment
#      blank lines and # comments allowed.
#
# Example (foreground, 1 target):
#   ./launch.sh --run-id rerun_001 --project /path/to/curve25519-dalek \
#       --vstd-root /path/to/vstd --results results-C \
#       src/edwards.rs
#
# Example (background, mixed result-dirs / budgets, via targets file):
#   cat > /tmp/targets <<EOF
#   results   | src/lemmas/field_lemmas/u64_5_as_nat_lemmas.rs | 60
#   results-C | src/edwards.rs                                  | 90
#   results-C | src/window.rs
#   EOF
#   ./launch.sh --detach --run-id rerun_002 \
#       --project /path/to/curve25519-dalek --vstd-root /path/to/vstd \
#       --targets-file /tmp/targets
#
# Watch the log:
#   tail -f launcher_<run-id>.log
#   tail -f launcher_<run-id>.log | grep --line-buffered '^MARKER'
#
# Each completed target emits one MARKER line:
#   MARKER target=<rel> start_admits=N end_admits=M success=BOOL \
#          end_reason=STR rounds_used=N duration_seconds=F.
# Final line: MARKER orchestrator=done.

set -u
set -o pipefail

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Required:
  --run-id <id>            run identifier (used in results paths and log name)
  --project <path>         Cargo root (forwarded to run.py --project)

Optional:
  --results <dir>          default results dir for positional targets (default: results)
  --vstd-root <path>       forward to run.py --vstd-root
  --rounds N               forward to run.py --rounds (default: 5)
  --budget-min N           default --max-task-minutes (per task; can override per-line)
  --model <name>           forward to run.py --model
  --experiment-mode <m>    run.py experiment mode: proof-only | spec-proof.
                           When set, each target is passed to run.py as both the
                           target AND its own --experiment-allow-edit (the layer
                           model: each file is the file the agent edits). proof-only
                           keeps the spec-integrity gate ON; spec-proof disables it
                           (run.py handles that coupling). Omit for normal admit-fill.
  --admit                  create the admit() skeleton in each target IN PLACE
                           before running run.py (overwrites bodies). Proof fn
                           bodies + inline proof{} blocks -> admit(); spec fns,
                           exec code, and axiom_* are preserved. Opt-in;
                           idempotent on already-admitted files. NOTE: this
                           resets any proofs already in the file.
  --admit-mode <m>         auto|fn-bodies|proof-blocks|both (default: auto —
                           lemmas/ & specs/ -> fn-bodies, else -> proof-blocks)
  --skip-existing          skip targets already proven (success) in their
                           results dir's proven_registry.json. Use to resume a
                           sweep: re-run the SAME command and only the not-yet-
                           done targets run. A 429-halted target is recorded
                           RATE_LIMITED (not in the registry), so it re-runs.
  --detach                 fully detach via POSIX setsid (Python start_new_session).
                           Required when launching from Claude Code's Bash tool;
                           survives session/tool teardown. Foreground by default.
  --log <path>             log file when --detach (default: ./launcher_<run-id>.log)
  --targets-file <path>    one-target-per-line spec (see header). Combine with
                           or use instead of positional targets.
  -h, --help               show this help
EOF
}

# ── Parse args ───────────────────────────────────────────────────────────────
RUN_ID=""
PROJECT=""
RESULTS_DEFAULT="results"
VSTD_ROOT=""
ROUNDS="5"
BUDGET_MIN=""
MODEL=""
EXPERIMENT_MODE=""
ADMIT="0"
ADMIT_MODE="auto"
SKIP_EXISTING="0"
DETACH="0"
LOG=""
TARGETS_FILE=""
TARGETS=()

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)        usage; exit 0 ;;
    --run-id)         RUN_ID="$2"; shift 2 ;;
    --project)        PROJECT="$2"; shift 2 ;;
    --results)        RESULTS_DEFAULT="$2"; shift 2 ;;
    --vstd-root)      VSTD_ROOT="$2"; shift 2 ;;
    --rounds)         ROUNDS="$2"; shift 2 ;;
    --budget-min)     BUDGET_MIN="$2"; shift 2 ;;
    --model)          MODEL="$2"; shift 2 ;;
    --experiment-mode) EXPERIMENT_MODE="$2"; shift 2 ;;
    --admit)          ADMIT="1"; shift ;;
    --admit-mode)     ADMIT_MODE="$2"; shift 2 ;;
    --skip-existing)  SKIP_EXISTING="1"; shift ;;
    --detach)         DETACH="1"; shift ;;
    --log)            LOG="$2"; shift 2 ;;
    --targets-file)   TARGETS_FILE="$2"; shift 2 ;;
    --) shift; while [ $# -gt 0 ]; do TARGETS+=("$1"); shift; done ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  TARGETS+=("$1"); shift ;;
  esac
done

[ -n "$RUN_ID" ]  || { echo "error: --run-id required" >&2; exit 2; }
[ -n "$PROJECT" ] || { echo "error: --project required" >&2; exit 2; }
[ -d "$PROJECT" ] || { echo "error: --project not a directory: $PROJECT" >&2; exit 2; }
if [ -n "$EXPERIMENT_MODE" ] \
   && [ "$EXPERIMENT_MODE" != "proof-only" ] \
   && [ "$EXPERIMENT_MODE" != "spec-proof" ]; then
  echo "error: --experiment-mode must be 'proof-only' or 'spec-proof'" >&2; exit 2
fi

LOG="${LOG:-./launcher_${RUN_ID}.log}"

# ── Detach: re-exec via Python's start_new_session (POSIX setsid) ───────────
# This is the only reliable way to survive Claude Code's Bash-tool teardown,
# which kills its entire child process group. macOS lacks the `setsid` binary,
# so we use Python (3.11+ already required by the repo).
if [ "$DETACH" = "1" ] && [ "${_LAUNCH_DETACHED:-0}" != "1" ]; then
  PY="${PYTHON:-$(command -v python3 || true)}"
  [ -n "$PY" ] || { echo "error: python3 not found on PATH" >&2; exit 3; }

  # Build re-entry argv with --detach stripped (so the child runs the loop)
  REENTRY_ARGS=()
  for a in "$@"; do
    [ "$a" = "--detach" ] && continue
    REENTRY_ARGS+=("$a")
  done
  # If the user passed flags via array we already consumed them — instead of
  # reconstructing perfectly, re-pass everything we parsed:
  REENTRY_ARGS=(
    --run-id   "$RUN_ID"
    --project  "$PROJECT"
    --results  "$RESULTS_DEFAULT"
    --rounds   "$ROUNDS"
    --log      "$LOG"
  )
  [ -n "$VSTD_ROOT" ]    && REENTRY_ARGS+=(--vstd-root    "$VSTD_ROOT")
  [ -n "$BUDGET_MIN" ]   && REENTRY_ARGS+=(--budget-min   "$BUDGET_MIN")
  [ -n "$MODEL" ]            && REENTRY_ARGS+=(--model           "$MODEL")
  [ -n "$EXPERIMENT_MODE" ]  && REENTRY_ARGS+=(--experiment-mode "$EXPERIMENT_MODE")
  [ "$ADMIT" = "1" ]         && REENTRY_ARGS+=(--admit)
  [ -n "$ADMIT_MODE" ]       && REENTRY_ARGS+=(--admit-mode      "$ADMIT_MODE")
  [ "$SKIP_EXISTING" = "1" ] && REENTRY_ARGS+=(--skip-existing)
  [ -n "$TARGETS_FILE" ] && REENTRY_ARGS+=(--targets-file "$TARGETS_FILE")
  for t in "${TARGETS[@]:-}"; do
    [ -n "$t" ] && REENTRY_ARGS+=("$t")
  done

  # Spawn detached. Output is the launcher log; stdin /dev/null.
  export _LAUNCH_LOG="$LOG"
  PID=$("$PY" - "$0" "${REENTRY_ARGS[@]}" <<'PYEOF'
import os, subprocess, sys
log_path = os.environ['_LAUNCH_LOG']
env = os.environ.copy()
env['_LAUNCH_DETACHED'] = '1'
p = subprocess.Popen(
    ['bash'] + sys.argv[1:],
    stdin=subprocess.DEVNULL,
    stdout=open(log_path, 'w'),
    stderr=subprocess.STDOUT,
    start_new_session=True,
    env=env,
)
print(p.pid)
PYEOF
)
  rc=$?
  [ $rc -eq 0 ] || { echo "error: detach spawn failed (rc=$rc)" >&2; exit $rc; }
  echo "$PID" > "${LOG%.log}.pid"
  echo "launched detached pid=$PID log=$LOG pid_file=${LOG%.log}.pid"
  echo "watch:  tail -f $LOG | grep --line-buffered '^MARKER'"
  exit 0
fi
# When detached, the Python launcher passes _LAUNCH_LOG via env so we know
# where to write. (Foreground mode also uses LOG below.)
export _LAUNCH_LOG="$LOG"

# ── Resolve target list ──────────────────────────────────────────────────────
SPEC_LINES=()
if [ -n "$TARGETS_FILE" ]; then
  [ -f "$TARGETS_FILE" ] || { echo "error: targets-file not found: $TARGETS_FILE" >&2; exit 2; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"                              # strip trailing comment
    line="$(echo -n "$line" | tr -d '[:space:]')"   # strip all whitespace
    [ -z "$line" ] && continue
    SPEC_LINES+=("$line")
  done < "$TARGETS_FILE"
fi
for t in "${TARGETS[@]:-}"; do
  [ -z "$t" ] && continue
  SPEC_LINES+=("${RESULTS_DEFAULT}|${t}|${BUDGET_MIN}")
done

[ "${#SPEC_LINES[@]:-0}" -gt 0 ] || { echo "error: no targets given" >&2; exit 2; }

# ── Per-target loop ──────────────────────────────────────────────────────────
# When detached, all output is already redirected to the log file by the
# Python launcher. When foreground, mirror to both log and stderr so the
# user sees progress live.
say() {
  if [ "${_LAUNCH_DETACHED:-0}" = "1" ]; then
    printf '%s\n' "$*"
  else
    printf '%s\n' "$*" | tee -a "$LOG"
  fi
}

# Count actionable (non-axiom) admit()s the SAME way the harness gate does:
# comment/string-aware and excluding `proof fn axiom_*` bodies (via
# skills/admit_inventory.py → lib.admits). This keeps the MARKER start/end
# counts aligned with run.py's notion of progress instead of a raw
# `grep -c 'admit()'`, which over-counts axiom-body admits and comment/string
# mentions. Falls back to grep only if the inventory skill can't run.
count_admits() {
  local f="$1" n
  n=$(python3 skills/admit_inventory.py "$f" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["non_axiom_count"])' 2>/dev/null)
  if [ -z "$n" ]; then
    n=$(grep -c 'admit()' "$f" 2>/dev/null || echo 0)
  fi
  printf '%s' "$n"
}

say "[$(date -u +%FT%TZ)] LAUNCH run_id=$RUN_ID pid=$$ ppid=$PPID targets=${#SPEC_LINES[@]} log=$LOG"

RC_TOTAL=0
for spec in "${SPEC_LINES[@]}"; do
  IFS='|' read -r results_dir rel budget <<< "$spec"
  [ -z "${results_dir:-}" ] && results_dir="$RESULTS_DEFAULT"
  [ -z "${budget:-}" ]      && budget="$BUDGET_MIN"
  [ -z "${rel:-}" ] && { say "skip: empty target in '$spec'"; continue; }

  if [[ "$rel" = /* ]]; then
    target="$rel"
    rel_for_log="${rel#${PROJECT}/}"
  else
    target="$PROJECT/$rel"
    rel_for_log="$rel"
  fi
  [ -f "$target" ] || { say "skip: target not found: $target"; continue; }
  task_id_root="$(basename "$rel" .rs)"

  # Optional resume: skip targets already proven (success) in this results
  # dir's proven_registry.json. Mirrors run_layer.py --skip-existing. The
  # registry is the trustworthy completion signal — a 429-halted target is
  # recorded RATE_LIMITED (not COMPLETE), so it is NOT in the registry and
  # will correctly re-run. Lets "continue a throttled sweep" be: re-run the
  # exact same command after the quota window resets.
  if [ "$SKIP_EXISTING" = "1" ]; then
    reg="${results_dir}/proven_registry.json"
    if [ -f "$reg" ] && python3 -c "
import json, sys
try:
    names = {p.get('name') for p in json.load(open(sys.argv[1])).get('proven', [])}
except Exception:
    sys.exit(1)
sys.exit(0 if sys.argv[2] in names else 1)
" "$reg" "$task_id_root"; then
      say "[$(date -u +%FT%TZ)] SKIP rel=$rel_for_log (already in ${reg##*/})"
      continue
    fi
  fi

  # Optional: create the admit() skeleton IN PLACE before the run. Opt-in
  # (--admit). Runs before start_admits is counted, so start_admits below
  # reflects the admitted skeleton the run actually begins from.
  if [ "$ADMIT" = "1" ]; then
    pre_admit=$(count_admits "$target")
    say "[$(date -u +%FT%TZ)] ADMIT begin rel=$rel_for_log mode=$ADMIT_MODE admits_before=$pre_admit"
    if [ "${_LAUNCH_DETACHED:-0}" = "1" ]; then
      python3 admit.py "$target" --in-place --mode "$ADMIT_MODE"
    else
      python3 admit.py "$target" --in-place --mode "$ADMIT_MODE" 2>&1 | tee -a "$LOG"
    fi
    arc=${PIPESTATUS[0]:-$?}
    [ "$arc" -ne 0 ] && say "ADMIT failed rel=$rel_for_log rc=$arc (continuing with file as-is)"
  fi

  start_admits=$(count_admits "$target")
  say "[$(date -u +%FT%TZ)] TARGET begin rel=$rel_for_log results=$results_dir start_admits=$start_admits budget_min=${budget:-auto}"

  CMD=(python3 run.py "$target"
       --rounds   "$ROUNDS"
       --run-id   "$RUN_ID"
       --results  "$results_dir"
       --project  "$PROJECT")
  [ -n "$VSTD_ROOT" ] && CMD+=(--vstd-root        "$VSTD_ROOT")
  [ -n "$MODEL" ]     && CMD+=(--model            "$MODEL")
  [ -n "$budget" ]    && CMD+=(--max-task-minutes "$budget")
  # Experiment mode: the target file IS the file the agent edits (layer model),
  # so pass it as its own --experiment-allow-edit. This renders run.py's
  # experiment-mode prompt block; for proof-only the spec-integrity gate stays on.
  [ -n "$EXPERIMENT_MODE" ] && CMD+=(--experiment-mode "$EXPERIMENT_MODE"
                                     --experiment-allow-edit "$target")

  if [ "${_LAUNCH_DETACHED:-0}" = "1" ]; then
    "${CMD[@]}"
  else
    "${CMD[@]}" 2>&1 | tee -a "$LOG"
  fi
  rc=${PIPESTATUS[0]:-$?}
  [ "$rc" -ne 0 ] && RC_TOTAL=$rc

  # rc 42 = run.py hit a 429 quota limit. Every remaining target would just
  # be rejected too until the window resets, so stop the whole sweep cleanly
  # instead of churning out no-op rounds. Re-run the same command (ideally
  # with --skip-existing) after the window reopens to continue.
  if [ "$rc" -eq 42 ]; then
    say "MARKER target=$rel_for_log rate_limited=1 end_reason=RATE_LIMITED"
    say "[$(date -u +%FT%TZ)] RATE LIMITED on $rel_for_log — aborting run "\
"(API 429). Re-run the same command after the quota window resets; "\
"add --skip-existing to resume from where this stopped."
    break
  fi

  end_admits=$(count_admits "$target")
  result_json="${results_dir}/${RUN_ID}/${task_id_root}/result.json"
  if [ -f "$result_json" ]; then
    summary=$(python3 -c "
import json, sys
try:
    d = json.load(open('$result_json'))
    print('success={} end_reason={} rounds_used={} duration_seconds={:.1f}'.format(
        d.get('success'), d.get('end_reason'), d.get('rounds_used'),
        float(d.get('duration_seconds') or 0)))
except Exception as e:
    print('result_json=PARSE_ERROR ({})'.format(e))
")
  else
    summary="result_json=MISSING rc=$rc"
  fi
  say "MARKER target=$rel_for_log start_admits=$start_admits end_admits=$end_admits $summary"
done

say "[$(date -u +%FT%TZ)] ALL DONE run_id=$RUN_ID rc=$RC_TOTAL"
say "MARKER orchestrator=done"
exit $RC_TOTAL
