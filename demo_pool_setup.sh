#!/usr/bin/env bash
# demo_pool_setup.sh — create K pre-warmed, isolated worktree slots for the
# website demo. Run ONCE (idempotent: existing slots are re-warmed, not
# recreated). The website owns a K-slot semaphore and, per request, passes a
# free slot's env triple to demo_decompress.sh:
#
#     DALEK_PROJECT=<slot.project> DALEK_GITROOT=<slot.gitroot> \
#     DALEK_RESULTS=<slot.results>  ./demo_decompress.sh --<mode> --run-id <id>
#
# Each slot is a full git worktree → isolated dep file + its own cargo target/
# lock; plus its own results root → no cumulative-JSON races. K bounds how many
# proof runs can be in flight at once (excess requests queue on the website's
# semaphore). K is also capped in practice by disk (K × ~1-3 GB target/) and by
# the single OAuth account's rate limit (all slots bill one token).
#
# For full website↔local decoupling, point --gitroot at a DEDICATED clone of
# the dalek repo (so a local commit / branch switch can't ripple into the pool).
#
# Usage:
#   ./demo_pool_setup.sh [--size 3] [--gitroot /tmp/dalek-baf] \
#                        [--pool-dir /tmp/dalek-demo-pool] [--ref main]
#
# Prints a JSON array of slots to stdout (progress goes to stderr).
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── machine-specific defaults (same prelude as demo_decompress.sh) ───────────
UV_PY_BIN="${DALEK_UV_PY_BIN:-/path/to/python3/bin}"
VERUS_DIR="${DALEK_VERUS_DIR:-/tmp/verus-rel/verus-arm64-macos}"
SIZE=3
GITROOT="/tmp/dalek-baf"
POOL_DIR="/tmp/dalek-demo-pool"
REF="main"
MEMBER="curve25519-dalek"   # cargo member subdir inside the worktree

die() { echo "demo_pool_setup: $*" >&2; exit 2; }
usage() { sed -n '2,30p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --size)     SIZE="$2";     shift 2 ;;
    --gitroot)  GITROOT="$2";  shift 2 ;;
    --pool-dir) POOL_DIR="$2"; shift 2 ;;
    --ref)      REF="$2";      shift 2 ;;
    -h|--help)  usage 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

export PATH="$UV_PY_BIN:$VERUS_DIR:$PATH"

# ── preflight ────────────────────────────────────────────────────────────────
command -v python3     >/dev/null || die "python3 not on PATH"
command -v cargo-verus >/dev/null || die "cargo-verus not on PATH ($VERUS_DIR missing?)"
[ -d "$GITROOT/.git" ] || git -C "$GITROOT" rev-parse --git-dir >/dev/null 2>&1 \
  || die "--gitroot is not a git repo: $GITROOT"

echo "demo_pool_setup: K=$SIZE  gitroot=$GITROOT  pool=$POOL_DIR  ref=$REF" >&2

SLOTS_JSON="["
for i in $(seq 0 $((SIZE - 1))); do
  WT="$POOL_DIR/wt-$i"
  PROJ="$WT/$MEMBER"
  RES="$WT/results"

  echo "── slot $i: $WT" >&2
  if [ ! -d "$WT" ]; then
    mkdir -p "$POOL_DIR"
    git -C "$GITROOT" worktree add --detach "$WT" "$REF" >&2
  else
    echo "   (worktree exists — reusing)" >&2
  fi
  [ -d "$PROJ" ] || die "member dir missing in slot: $PROJ"

  # toolchain pin (main does not carry rust-toolchain.toml) + results root
  printf '[toolchain]\nchannel = "1.92.0"\n' > "$WT/rust-toolchain.toml"
  mkdir -p "$RES"

  # warm: compile vstd + deps into this slot's target/ so the first real,
  # module-scoped check doesn't spuriously fail. ~2-3 min cold, then cached.
  echo "   warming (cold ~2-3 min)…" >&2
  ( cd "$PROJ" && cargo verus verify -p curve25519-dalek >/dev/null 2>&1 ) || true
  mkdir -p "$PROJ/target"; : > "$PROJ/target/.demo_warmed"
  echo "   ready." >&2

  sep=$([ "$i" -gt 0 ] && echo "," || echo "")
  SLOTS_JSON+="$sep$(python3 -c '
import json,sys
print(json.dumps({"slot":int(sys.argv[1]),"project":sys.argv[2],
                  "gitroot":sys.argv[3],"results":sys.argv[4]}))
' "$i" "$PROJ" "$WT" "$RES")"
done
SLOTS_JSON+="]"

echo "demo_pool_setup: $SIZE slot(s) ready." >&2
echo "$SLOTS_JSON" | python3 -m json.tool
