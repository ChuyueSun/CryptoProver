#!/usr/bin/env bash
#
# One-shot preflight, run in a throwaway container BEFORE launching N expensive
# agents (T112: the ro shared-registry mount is an OPTIMIZATION, valid only if the
# offline cache actually resolves with the exact mounted env). A ro cache miss
# fails hard, so we prove it once here instead of discovering it per-agent.
#
# Run it the same way the real agents run — same image, same env, same mounts:
#   docker run --rm --init \
#       -e CARGO_NET_OFFLINE=true -e CARGO_HOME=/opt/cargo-home \
#       -e CARGO_TARGET_DIR=/opt/cargo-target \
#       [-v <shared-registry>:/opt/cargo-home/registry:ro] \
#       -v <a-sealed-work-vol>:/work \
#       dalek-harness:v1 bash /opt/harness/docker/preflight.sh /work/curve25519-dalek
#
# Exit 0 => offline cache + sealed work are good; launch the sweep.
# Exit !=0 => DO NOT mount the ro registry / DO NOT launch; fall back to the baked
#            per-container CARGO_HOME (still correct, just more disk).
set -euo pipefail

project=${1:?usage: preflight.sh <member-project-dir e.g. /work/curve25519-dalek>}
fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 1; }

echo "preflight: CARGO_NET_OFFLINE=${CARGO_NET_OFFLINE:-unset} CARGO_HOME=${CARGO_HOME:-unset} CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-unset}"

# 1) The /work tree must be the sealed baseline (full seal contract, mirroring
#    seal_into_volume.sh + run.py's _is_sealed_worktree / sealed-startup fsck audit).
work_root=$(git -C "$project" rev-parse --show-toplevel 2>/dev/null) || fail "$project is not a git repo"
sub=$(git -C "$work_root" log -1 --format=%s 2>/dev/null) || fail "$work_root has no commit"
case "$sub" in *"history sealed"*) ;; *) fail "/work HEAD not sealed: '$sub'";; esac
git -C "$work_root" rev-parse -q --verify 'HEAD^' >/dev/null 2>&1 \
    && fail "/work HEAD has a parent — not an orphan seal"
fsck=$(git -C "$work_root" fsck --no-reflogs --unreachable --no-progress 2>&1 || true)
[ -z "$fsck" ] || fail "/work fsck not clean (dangling objects):\n$fsck"
[ -z "$(git -C "$work_root" for-each-ref refs/heads refs/remotes refs/tags)" ] \
    || fail "/work has stray refs/remotes beyond detached HEAD"

# 2) Offline dependency resolution: cheap proof the cache is COMPLETE for this lock.
#    `cargo fetch --offline` resolves every dep from cache WITHOUT building. A miss
#    here is exactly the hard failure we must catch before spawning agents.
( cd "$project" && cargo fetch --offline ) \
    || fail "cargo fetch --offline missed the cache — registry mount/env incomplete"

# 3) Toolchain sanity.
verus --version >/dev/null || fail "verus not on PATH"
cargo verus --help >/dev/null 2>&1 || fail "cargo verus subcommand missing"

# 4) Bounded offline cargo-verus EXERCISE (codex T112 16:47 #2): prove the toolchain
#    + warm target + offline cache actually drive a real verify under the exact
#    image/env/mounts — not just dep resolution. We FAIL only on an offline/network
#    resolution error; proof errors or a timeout are fine (they still prove offline
#    resolved and verus ran). This is what makes the ro-registry mount safe to enable.
to=${PREFLIGHT_VERUS_TIMEOUT:-300}
echo "preflight: bounded (${to}s) offline cargo verus verify on $(basename "$project") ..."
# NOTE: a nonzero command-substitution in an assignment aborts under `set -e` BEFORE
# `vrc=$?` runs (codex T112 16:54). We WANT to keep a nonzero rc here — proof errors
# and timeouts are acceptable — so disable -e just around the capture.
set +e
vout=$( cd "$project" && timeout "$to" cargo verus verify -p curve25519-dalek 2>&1 ); vrc=$?
set -e
if printf '%s\n' "$vout" | grep -qiE 'offline|failed to (download|get|load)|unable to (update|get)|needs to be updated|no matching package'; then
    printf '%s\n' "$vout" | tail -20 >&2
    fail "cargo verus verify hit an OFFLINE/network resolution error — registry cache/mount incomplete"
fi
echo "preflight: cargo verus exercised (rc=$vrc) — offline resolution OK (proof errors/timeout are fine here)"

echo "PREFLIGHT OK — offline cache resolves, work sealed, toolchain live."
