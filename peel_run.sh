#!/usr/bin/env bash
# peel_run.sh — the thin bridge from peel (build-side) to run.py (run-side).
#
# ONE command takes a peel manifest to a launched proof run:
#
#     manifest ──peel.py --worktree──▶ isolated peeled checkout
#                                      + JSON {project, experiment_mode,
#                                              editable_files, pin}
#              ──this script reads it──▶ run.py --experiment-mode <mode>
#                                              --experiment-allow-edit <files…>
#
# peel.py owns the *what to strip* (a deterministic, depth-keyed init state +
# the pin rule); run.py owns the *run* (rounds, gates, the agent loop). This
# script is the glue the two were designed for: peel already emits exactly the
# two fields run.py needs (`experiment_mode`, `editable_files`), so the bridge
# is a JSON read, not a translation layer. See docs/spec_gen_runbook.md.
#
# It deliberately does NOT reset-in-place like demo_decompress.sh /
# launch_specgen.sh. peel's worktree model is one *fresh* checkout per init
# state (keyed by --run-id), so two launched runs never share a tree. Fresh
# worktree creation/sealing is serialized per source repo because git metadata
# is shared; after launch, parallel runs are safe if each has its own worktree
# and --results root.
#
# Usage:
#   ./peel_run.sh --manifest peel_manifests/decompress_bridge_full.json \
#       --depth 2 --run-id peel_001 --detach
#   ./peel_run.sh --manifest M --depth N --surface          # preview, no worktree
#   ./peel_run.sh --manifest M --depth N --run-id ID --dry-run   # build+argv, no launch
#   ./peel_run.sh --run-id ID --remove                      # tear the worktree down
#   # RESUME: run on an EXISTING peeled worktree (no rebuild — continue from its
#   # current, partially-reconstructed state). Editable set/mode come from M.
#   ./peel_run.sh --manifest M --reuse-worktree /path/to/wt \
#       --run-id resume_001 --detach
#
# Manifest keys consumed here (everything else is peel.py's — see peel.py docstr):
#   target          required to launch — the run.py anchor, relative to the
#                   worktree root (e.g. "curve25519-dalek/src/ristretto.rs").
#   depth           the peel depth this cut needs; used when --depth is omitted.
#   experiment_mode passed through by peel into its JSON → run.py --experiment-mode.
#   pin             passed through; --pin overrides it.
#
# Env overrides (same names/spirit as demo_decompress.sh / launch_specgen.sh):
#   DALEK_SRCREPO     git repo root to worktree FROM (the proven tree)
#                     (default: autodetect $HOME/dalek-lite, ./dalek-lite, …)
#   DALEK_PEEL_WT_BASE  base dir for per-run worktrees   (default $TMPDIR/dalek-peel)
#   DALEK_SRCREF      clean proven ref to check out      (default main; --ref wins)
#   DALEK_VSTD        vstd source dir (default: autodetect under ~/.cargo/git)
#   DALEK_RESULTS     results root                       (default <harness>/results)
#   DALEK_TAP         set to 0/off to disable claude-tap capture (default: ON)
#   DALEK_TAP_PORT    tap proxy port                     (default 58960)
#   DALEK_TAP_LIVE_PORT  tap dashboard port              (default 8799)
#   DALEK_UV_PY_BIN, DALEK_VERUS_DIR  toolchain dirs prepended to PATH
#   CLAUDE_CODE_OAUTH_TOKEN | DALEK_DEMO_TOKEN_FILE      headless auth (else keychain)
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── portable defaults (override via env) ─────────────────────────────────────
# These were once hardcoded to one laptop's absolute paths (a Mac /Users/... +
# /private/tmp/... layout), which made the script unrunnable on any other host
# (e.g. a cloud VM). They now AUTODETECT from conventional locations; the env
# vars above still take outright priority. Each helper echoes a path or nothing
# and never trips `set -e`.
_first_existing() { local p; for p in "$@"; do [ -e "$p" ] && { printf '%s\n' "$p"; return 0; }; done; return 0; }
_glob_dir()       { ls -d "$@" 2>/dev/null | head -1 || true; }
_verus_bin_dir()  { local f; f="$(find "$HOME/verus-rel" /tmp/verus-rel -name cargo-verus -type f 2>/dev/null | head -1 || true)"; [ -n "$f" ] && dirname "$f" || true; }
_src_repo()       { local d; for d in "$HOME/dalek-lite" "$PWD/dalek-lite" /private/tmp/dalek-baf; do [ -d "$d/.git" ] && { printf '%s\n' "$d"; return 0; }; done; return 0; }

UV_PY_BIN="${DALEK_UV_PY_BIN:-$(_glob_dir "$HOME"/.local/share/uv/python/cpython-3.1[1-9]*/bin)}"
VERUS_DIR="${DALEK_VERUS_DIR:-$(_verus_bin_dir)}"
SRCREPO="${DALEK_SRCREPO:-$(_src_repo)}"
WT_BASE="${DALEK_PEEL_WT_BASE:-${TMPDIR:-/tmp}/dalek-peel}"
SRCREF="${DALEK_SRCREF:-main}"
VSTD="${DALEK_VSTD:-$(_glob_dir "$HOME"/.cargo/git/checkouts/verus-*/*/source/vstd)}"
RESULTS_ROOT="${DALEK_RESULTS:-$HARNESS_DIR/results}"

# ── args ─────────────────────────────────────────────────────────────────────
MANIFEST="" ; DEPTH="" ; RUN_ID="" ; PIN="" ; REF="$SRCREF"
ROUNDS=10 ; BUDGET=180 ; MODEL="opus"
SURFACE=0 ; DRYRUN=0 ; DETACH=0 ; REMOVE=0 ; REUSE_WT=""
# claude-tap capture: ON by default for peel runs — route the claude subprocess
# through the local tap proxy so each run becomes a browsable trace session in
# the dashboard. Disable per-run with --no-tap, or globally with DALEK_TAP=0.
TAP=1
case "${DALEK_TAP:-}" in 0|off|false|no|OFF|FALSE|No) TAP=0 ;; esac
TAP_PORT="${DALEK_TAP_PORT:-58960}"

die() { echo "peel_run: $*" >&2; exit 2; }
usage() { sed -n '2,55p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --manifest)  MANIFEST="$2"; shift 2 ;;
    --depth)     DEPTH="$2";    shift 2 ;;
    --run-id)    RUN_ID="$2";   shift 2 ;;
    --pin)       PIN="$2";      shift 2 ;;
    --ref)       REF="$2";      shift 2 ;;
    --rounds)    ROUNDS="$2";   shift 2 ;;
    --budget)    BUDGET="$2";   shift 2 ;;
    --model)     MODEL="$2";    shift 2 ;;
    --reuse-worktree) REUSE_WT="$2"; shift 2 ;;
    --tap)       TAP=1;        shift ;;
    --no-tap)    TAP=0;        shift ;;
    --surface)   SURFACE=1;     shift ;;
    --dry-run)   DRYRUN=1;      shift ;;
    --detach)    DETACH=1;      shift ;;
    --remove)    REMOVE=1;      shift ;;
    -h|--help)   usage 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

# ── env prelude (toolchain + auth) ───────────────────────────────────────────
export PATH="$UV_PY_BIN:$VERUS_DIR:$PATH"
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -n "${DALEK_DEMO_TOKEN_FILE:-}" ] && [ -f "$DALEK_DEMO_TOKEN_FILE" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat "$DALEK_DEMO_TOKEN_FILE")"; export CLAUDE_CODE_OAUTH_TOKEN
fi
command -v python3 >/dev/null || die "python3 not on PATH ($UV_PY_BIN missing?)"

# Default --depth from the manifest's "depth" key when omitted (each manifest
# declares the depth its cut needs; --depth on the CLI overrides it).
manifest_depth() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("depth") or "")' "$1"
}
if [ -z "$DEPTH" ] && [ -n "$MANIFEST" ] && [ -f "$MANIFEST" ]; then
  DEPTH="$(manifest_depth "$MANIFEST")"
fi

peel_lock_file() {
  python3 - "$1" <<'PY'
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

repo = sys.argv[1]
try:
    common = subprocess.check_output(
        ["git", "-C", repo, "rev-parse", "--git-common-dir"],
        text=True,
    ).strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (Path(repo) / common_path)
    key_src = str(common_path.resolve())
except Exception:
    key_src = os.path.realpath(repo)

lock_dir = Path(tempfile.gettempdir()) / "dalek-lite-mvp-peel-locks"
lock_dir.mkdir(parents=True, exist_ok=True)
key = hashlib.sha256(key_src.encode()).hexdigest()[:16]
print(lock_dir / f"{key}.lock")
PY
}

build_peel_worktree_locked() {
  local lock_file="$1"; shift
  local wt="$1"; shift
  local srcrepo="$1"; shift
  python3 - "$lock_file" "$HARNESS_DIR" "$wt" "$srcrepo" "$@" <<'PY'
import fcntl
import os
import shutil
import subprocess
import sys

lock_file, harness_dir, wt, srcrepo = sys.argv[1:5]
peel_args = sys.argv[5:]
peel_py = os.path.join(harness_dir, "peel.py")

os.makedirs(os.path.dirname(lock_file), exist_ok=True)
with open(lock_file, "w") as lock:
    print(f"peel_run: waiting for worktree lock {lock_file}", file=sys.stderr)
    fcntl.flock(lock, fcntl.LOCK_EX)
    print(f"peel_run: acquired worktree lock {lock_file}", file=sys.stderr)

    if os.path.exists(wt):
        subprocess.run(
            [sys.executable, peel_py, "--worktree", wt, "--gitroot", srcrepo, "--remove"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        shutil.rmtree(wt, ignore_errors=True)

    proc = subprocess.run(
        [sys.executable, peel_py, *peel_args],
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    sys.exit(proc.returncode)
PY
}

# ── claude-tap capture (route claude through the local trace proxy) ──────────
# Best-effort, mirroring run.py's wire-proxy philosophy: tracing must NEVER fail
# a proof run. We (re)start the tap proxy on a FIXED port via bounce_tap.sh so
# each run.py invocation gets its OWN session (claude-tap keys sessions per proxy
# lifetime), then export ANTHROPIC_BASE_URL. run.py copies os.environ and only
# overrides the base URL under --wire-log (never passed here), so the var flows
# straight to the claude subprocess and its subagents. On ANY failure we warn and
# leave the env untouched → the run proceeds straight to api.anthropic.com.
setup_tap() {
  [ "$TAP" = "1" ] || return 0
  local tap_bin; tap_bin="$(command -v claude-tap || echo "$HOME/.local/bin/claude-tap")"
  if [ ! -x "$tap_bin" ]; then
    echo "peel_run: tap requested but claude-tap not found — continuing WITHOUT tap" >&2
    return 0
  fi
  export PATH="$(dirname "$tap_bin"):$PATH"   # bounce_tap.sh calls `claude-tap`
  export DALEK_TAP_PORT="$TAP_PORT"
  if bash "$HARNESS_DIR/bounce_tap.sh" >&2; then
    export ANTHROPIC_BASE_URL="http://127.0.0.1:$TAP_PORT"
    echo "peel_run: tap ON — claude routed through 127.0.0.1:$TAP_PORT (dashboard :${DALEK_TAP_LIVE_PORT:-8799})" >&2
  else
    echo "peel_run: tap proxy did not start — continuing WITHOUT tap" >&2
  fi
}

# ── surface: preview the cut, no worktree, no launch ─────────────────────────
if [ "$SURFACE" = "1" ]; then
  [ -n "$MANIFEST" ] || die "--surface requires --manifest"
  [ -n "$DEPTH" ]    || die "--surface requires --depth (none on CLI or in manifest)"
  exec python3 "$HARNESS_DIR/peel.py" --surface --manifest "$MANIFEST" --depth "$DEPTH"
fi

[ -n "$RUN_ID" ] || die "--run-id required (omit only with --surface)"
# RUN_ID is interpolated into $WT and an `rm -rf` path — reject anything that
# could escape $WT_BASE (slashes, '..').
case "$RUN_ID" in
  */*|*..*) die "invalid --run-id (no '/' or '..'): $RUN_ID" ;;
esac
WT="$WT_BASE/$RUN_ID"

# ── remove: tear the per-run worktree down ───────────────────────────────────
if [ "$REMOVE" = "1" ]; then
  python3 "$HARNESS_DIR/peel.py" --worktree "$WT" --gitroot "$SRCREPO" --remove
  exit 0
fi

# ── launch path: preflight ───────────────────────────────────────────────────
[ -n "$MANIFEST" ] || die "--manifest required to launch"
[ -n "$DEPTH" ]    || die "--depth required to launch (none on CLI or in manifest)"
[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
command -v cargo-verus >/dev/null || die "cargo-verus not on PATH ($VERUS_DIR missing?)"
command -v claude      >/dev/null || die "claude not on PATH"
# Source repo only needed to BUILD a fresh worktree; resume mode reuses one.
[ -n "$REUSE_WT" ] || [ -d "$SRCREPO" ] || die "source repo missing: $SRCREPO (set DALEK_SRCREPO)"

TARGET_REL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("target") or "")' "$MANIFEST")"
[ -n "$TARGET_REL" ] || die "manifest has no \"target\" key (run.py needs an anchor file)"

# Manifest fingerprint: bind a built worktree to the manifest+depth it was peeled
# with, so --reuse-worktree can't silently run a DIFFERENT editable/pin set
# against it (a wrong manifest could mark a frozen pin editable). Stored at
# build, checked on reuse.
MANIFEST_SHA="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:16])' "$MANIFEST")-d$DEPTH"
SHA_FILE_REL=".peel_manifest_sha"

# ── obtain the worktree + run.py handoff ─────────────────────────────────────
# Two paths: REUSE an existing peeled worktree (resume — no rebuild, continue
# from its current state), or BUILD a fresh one from $REF. Both yield the same
# four handoff vars: WT_R, PROJECT, EXPMODE, EDITABLE_ABS[].
if [ -n "$REUSE_WT" ]; then
  [ -d "$REUSE_WT" ] || die "reuse worktree missing: $REUSE_WT"
  WT_R="$REUSE_WT"
  # Guard: the reused worktree must have been peeled with THIS manifest+depth.
  STORED_SHA="$(cat "$WT_R/$SHA_FILE_REL" 2>/dev/null || true)"
  if [ -z "$STORED_SHA" ]; then
    echo "peel_run: WARNING — $WT_R has no $SHA_FILE_REL (legacy worktree); "\
"cannot verify it matches $MANIFEST. Proceeding on trust." >&2
  elif [ "$STORED_SHA" != "$MANIFEST_SHA" ]; then
    die "manifest mismatch: $WT_R was peeled with $STORED_SHA but --manifest+--depth hash to $MANIFEST_SHA. Refusing to reuse (wrong editable/pin set)."
  fi
  # Derive straight from the manifest (no peel.py — the worktree already exists).
  EXPMODE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("experiment_mode") or "")' "$MANIFEST")"
  PROJECT="$WT_R/${TARGET_REL%%/*}"   # first path component of target = cargo member dir
  EDITABLE_ABS=()
  while IFS= read -r line; do EDITABLE_ABS+=("$line"); done < <(python3 - "$MANIFEST" "$WT_R" <<'PY'
import json, os, sys
m = json.load(open(sys.argv[1])); wt = sys.argv[2]
for f in m.get("files", []):
    print(os.path.join(wt, f["path"]))
PY
)
  echo "peel_run: REUSE existing worktree $WT_R (mode $EXPMODE, no rebuild)" >&2
else
  # Build a fresh peeled worktree per run-id. Idempotent: a stale worktree at
  # $WT is removed first, then rebuilt from $REF. The whole remove/build/seal
  # sequence is serialized per source repo because git worktree metadata and
  # orphan-branch refs are shared across all worktrees of that repo.
  PEEL_ARGS=( --worktree "$WT" --gitroot "$SRCREPO" --ref "$REF"
              --depth "$DEPTH" --manifest "$MANIFEST" )
  [ -n "$PIN" ] && PEEL_ARGS+=( --pin "$PIN" )
  echo "peel_run: building peel worktree at $WT (depth $DEPTH, ref $REF)…" >&2
  PEEL_LOCK="$(peel_lock_file "$SRCREPO")"
  PEEL_JSON="$(build_peel_worktree_locked "$PEEL_LOCK" "$WT" "$SRCREPO" "${PEEL_ARGS[@]}")" || {
    echo "$PEEL_JSON" >&2; die "peel worktree build failed"; }
  # Use peel's RESOLVED worktree (e.g. /private/tmp on macOS) for every derived
  # path so run.py never sees a /tmp-vs-/private/tmp split between target/project.
  read -r WT_R PROJECT EXPMODE PEEL_OK < <(python3 - "$PEEL_JSON" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(d.get("worktree", ""), d.get("project", ""),
      d.get("experiment_mode") or "", d.get("okay"))
PY
)
  [ "$PEEL_OK" = "True" ] || { echo "$PEEL_JSON" >&2; die "peel reported not-okay"; }
  # editable_files are worktree-relative; run.py wants paths under the worktree.
  # (read loop, not `mapfile` — macOS ships bash 3.2, which lacks mapfile.)
  EDITABLE_ABS=()
  while IFS= read -r line; do EDITABLE_ABS+=("$line"); done < <(python3 - "$PEEL_JSON" "$WT_R" <<'PY'
import json, os, sys
d = json.loads(sys.argv[1]); wt = sys.argv[2]
for rel in d.get("editable_files", []):
    print(os.path.join(wt, rel))
PY
)
  # Fingerprint the worktree so a later --reuse-worktree can verify the manifest.
  printf '%s\n' "$MANIFEST_SHA" > "$WT_R/$SHA_FILE_REL" 2>/dev/null || true
fi

# ── common validation (both paths) ───────────────────────────────────────────
[ -n "$EXPMODE" ] || die "manifest declared no experiment_mode — set it (proof-only|spec-proof|contract-only|bridge-specs|bridge-full|field-floor)"
[ -d "$PROJECT" ] || die "project dir missing: $PROJECT"
[ "${#EDITABLE_ABS[@]}" -gt 0 ] || die "no editable_files derived"
TARGET="$WT_R/$TARGET_REL"
[ -f "$TARGET" ] || die "target not in worktree: $TARGET"

# ── assemble run.py argv (experiment_mode → gate) ────────────────────────────
mkdir -p "$RESULTS_ROOT"
CMD=( python3 "$HARNESS_DIR/run.py" "$TARGET"
      --project "$PROJECT" --run-id "$RUN_ID"
      --rounds "$ROUNDS" --max-task-minutes "$BUDGET" --model "$MODEL"
      --results "$RESULTS_ROOT" --vstd-root "$VSTD"
      --experiment-mode "$EXPMODE"
      --experiment-allow-edit "${EDITABLE_ABS[@]}" )
# spec-proof has the agent (re)write contracts, so the header gate must be OFF.
[ "$EXPMODE" = "spec-proof" ] && CMD+=( --no-spec-gate )

TARGET_ID="$(basename "$TARGET" .rs)"
RESULTS_DIR="$RESULTS_ROOT/$RUN_ID/$TARGET_ID"
LOG="$HARNESS_DIR/launcher_peel_${RUN_ID}.log"

# dry-run: show what would launch (the worktree IS built; only the launch is
# skipped) — exit before the ~40s vstd warm, which only the real run needs.
if [ "$DRYRUN" = "1" ]; then
  echo "MODE $EXPMODE"
  echo "TAP $([ "$TAP" = 1 ] && echo "on → http://127.0.0.1:$TAP_PORT" || echo off)"
  echo "WORKTREE $WT_R"
  echo "PROJECT $PROJECT"
  echo "TARGET $TARGET"
  printf 'EDITABLE %s\n' "${EDITABLE_ABS[@]}"
  echo "ARGV ${CMD[*]}"
  exit 0
fi

# ── one-time vstd/build warm (cold module-scoped check spuriously fails) ──────
WARM_SENTINEL="$PROJECT/target/.peel_warmed"
if [ ! -f "$WARM_SENTINEL" ]; then
  echo "peel_run: warming verus build (one-time, ~40s)…" >&2
  ( cd "$PROJECT" && cargo verus verify -p curve25519-dalek >/dev/null 2>&1 ) || true
  mkdir -p "$(dirname "$WARM_SENTINEL")"; : > "$WARM_SENTINEL"
fi

# Start/refresh the tap proxy and export ANTHROPIC_BASE_URL just before launch,
# so BOTH the detached re-exec and the foreground exec inherit it.
setup_tap

# ── launch ───────────────────────────────────────────────────────────────────
if [ "$DETACH" = "1" ]; then
  # re-exec via Python start_new_session (POSIX setsid) so the run survives the
  # caller's process-group teardown. Same mechanism as demo_decompress.sh.
  export _PEEL_LOG="$LOG"
  PID=$(cd "$HARNESS_DIR" && python3 - "${CMD[@]}" <<'PYEOF'
import os, subprocess, sys
p = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL,
                     stdout=open(os.environ['_PEEL_LOG'], 'w'),
                     stderr=subprocess.STDOUT, start_new_session=True)
print(p.pid)
PYEOF
)
  echo "$PID" > "${LOG%.log}.pid"
  echo "RUN_ID $RUN_ID"
  echo "RESULTS $RESULTS_DIR"
  echo "MODE $EXPMODE"
  echo "TAP ${ANTHROPIC_BASE_URL:-off}"
  echo "LOG $LOG"
  echo "PID $PID"
  exit 0
fi

# foreground: exec run.py so its exit code is the proof outcome.
echo "RUN_ID $RUN_ID"
echo "RESULTS $RESULTS_DIR"
echo "MODE $EXPMODE"
exec "${CMD[@]}"
