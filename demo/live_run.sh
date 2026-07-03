#!/usr/bin/env bash
# Live companion to demo.sh — kicks off a real proof run on one tiny target
# (elligator_lemmas, 1 admit). Run this in a SECOND terminal at Beat 1.
# Audience will see the agent's actual round/step output and the final diff.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---- config ----
DALEK_WORKTREE="/tmp/dalek-live-demo"
TARGET_REL="curve25519-dalek/src/lemmas/ristretto_lemmas/elligator_lemmas.rs"
TARGET="$DALEK_WORKTREE/$TARGET_REL"
VSTD="/path/to/verus/vstd"
RUN_ID="live_demo_$(date +%H%M%S)"

bold=$'\033[1m'; dim=$'\033[2m'; cyan=$'\033[36m'
green=$'\033[32m'; yellow=$'\033[33m'; reset=$'\033[0m'

say() { printf '%s%s%s\n' "$cyan" "$1" "$reset"; }
warn() { printf '%s%s%s\n' "$yellow" "$1" "$reset" >&2; }

# ---- pre-flight ----
if [ ! -f "$TARGET" ]; then
    warn "ERROR: $TARGET missing."
    warn "Re-create the worktree:  git -C /path/to/dalek-lite worktree add $DALEK_WORKTREE eval/admitted-start"
    exit 1
fi

# Restore the file to the admitted baseline (in case a prior run modified it).
git -C "$DALEK_WORKTREE" checkout -- "$TARGET_REL" 2>&1 | sed 's/^/  /'

n_admits=$(grep -c 'admit()' "$TARGET" || true)
if [ "$n_admits" = "0" ]; then
    warn "ERROR: target has no admit() — nothing for the agent to do."
    exit 1
fi

# ---- intro ----
clear
say "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
say "  LIVE PROOF RUN — running NOW, on this machine"
say "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "  target:   lemmas::ristretto_lemmas::elligator_lemmas"
echo "  admits:   $n_admits  (the proof we are about to fill in)"
echo "  budget:   up to 5 minutes"
echo "  expected: ~1-2 minutes (97s in our recorded baseline)"
echo
echo "  $(printf '%sStarting in 3s... (^C to abort)%s' "$dim" "$reset")"
sleep 3
echo

# ---- run ----
# Launch run.py in the background. Its stdout goes to a log file; the live
# stream is parsed directly from the per-round JSONL as it grows.
t0=$(date +%s)
LOG="/tmp/live-run-$RUN_ID.log"
JSONL="results/$RUN_ID/elligator_lemmas/claude_raw/round_1.jsonl"
mkdir -p "$(dirname "$JSONL")"

python3 run.py "$TARGET" \
    --vstd-root "$VSTD" \
    --run-id "$RUN_ID" \
    --rounds 5 \
    --max-task-minutes 5 \
    > "$LOG" 2>&1 &
RUN_PID=$!

# Stream agent activity in real time. Exits when the final result event fires.
python3 demo/_live_stream.py "$JSONL" --wait 60
stream_rc=$?

# Wait for run.py to finish writing result.json (it's already near-done).
wait "$RUN_PID" 2>/dev/null
elapsed=$(( $(date +%s) - t0 ))

# ---- post-run summary ----
echo
result_json="results/$RUN_ID/elligator_lemmas/result.json"
if [ -f "$result_json" ]; then
    end_reason=$(python3 -c "import json; print(json.load(open('$result_json'))['end_reason'])")
    success=$(python3 -c "import json; print(json.load(open('$result_json'))['success'])")
    if [ "$success" = "True" ]; then
        say "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        printf '%s%s  ✅ PROOF COMPLETE  in %ss  (end_reason=%s)%s\n' \
            "$bold" "$green" "$elapsed" "$end_reason" "$reset"
        say "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo
        echo "  $(printf '%sWhat the agent wrote:%s' "$dim" "$reset")"
        echo
        git -C "$DALEK_WORKTREE" --no-pager diff -- "$TARGET_REL" | head -60
    else
        warn ""
        warn "  ❌ end_reason=$end_reason  (expected COMPLETE)"
        warn "  See results/$RUN_ID/elligator_lemmas/cli.log for details."
    fi
else
    warn "ERROR: no result.json written — see /tmp/live-run.log"
fi
