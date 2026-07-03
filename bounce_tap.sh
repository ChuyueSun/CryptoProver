#!/usr/bin/env bash
# Restart the claude-tap proxy on a FIXED port so each demo run gets its OWN
# trace session — each becomes a separate, browsable thread in the dashboard.
#
# Why a fixed port: the website sets one ANTHROPIC_BASE_URL (→ this port) on the
# node server, so the URL must stay stable. A fresh proxy *lifetime* on the same
# port = a fresh session id (claude-tap keys sessions per proxy start), while the
# DB persists every past session so the dashboard's /api/sessions lists them all.
#
# Called by server/specProof.mjs before each run when DALEK_TAP is set. Safe to
# run standalone too.
set -uo pipefail
PORT="${DALEK_TAP_PORT:-58960}"
LIVE="${DALEK_TAP_LIVE_PORT:-8799}"
OUT="${DALEK_TAP_OUT:-/tmp/tap-traces}"
LOG="${DALEK_TAP_LOG:-/tmp/claude-tap.log}"

# Kill the proxy currently holding PORT (if any), wait for the port to free.
OLD=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $2}')
[ -n "$OLD" ] && kill "$OLD" 2>/dev/null
for _ in $(seq 1 25); do
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
  sleep 0.2
done

# Start a fresh proxy (new session); same live dashboard port.
nohup claude-tap --tap-no-launch --tap-port "$PORT" --tap-live --tap-live-port "$LIVE" \
  --tap-output-dir "$OUT" --tap-store-stream-events --tap-no-update-check --tap-no-open \
  >> "$LOG" 2>&1 &

# Wait until it binds before returning (so the run's claude connects to it).
for _ in $(seq 1 60); do
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && { echo "tap bounced → new session on :$PORT"; exit 0; }
  sleep 0.2
done
echo "bounce_tap: proxy did not bind on :$PORT" >&2
exit 1
