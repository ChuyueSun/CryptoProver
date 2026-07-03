#!/usr/bin/env bash
#
# Materialize a per-agent /work volume as a SELF-CONTAINED, history-sealed git repo
# with an ISOLATED object store — the T112 consensus design.
#
# Why not just bind-mount the peel worktree?
#   A `git worktree`'s .git is a pointer into the SHARED object store, which still
#   holds the proven/ground-truth objects (reachable via reflog/sha). Mounting it
#   either breaks git in the container (-> frozen-audit fail-closed -> FROZEN_EDIT)
#   or drags the full proven history in. Archiving the peeled tree into a fresh
#   `git init` gives a store that contains ONLY the sealed commit: HEAD == peeled
#   baseline (so run.py's frozen-file audit + revert work), and there is NO path —
#   no reflog, no dangling sha, no main ref — back to the proven source. This is
#   strictly cleaner than peel's host seal, whose documented residual
#   (peel.py:259-265) is exactly the shared store + detach reflog we omit here.
#
# Usage:  seal_into_volume.sh <source-worktree> <dest-work-volume>
#
# We copy the SOURCE WORKING-TREE BYTES (excluding .git / target/), NOT `git archive
# HEAD`. This is codex's T112 16:29 correction and it matters: the documented
# `admit.py --worktree --ref main --admit-target …` path checks out the proven ref at
# HEAD (admit.py:177) and writes the admitted skeleton to the WORKING TREE WITHOUT
# committing (admit.py:180-187) — so there `git archive HEAD` would emit the PROVEN
# source, reintroducing the very oracle we are closing. Copying the working tree
# captures the intended starting state regardless of whether the source builder
# committed it (peel.py's sealed path) or left it as unstaged edits (admit.py), and
# excluding .git guarantees no proven history travels into the isolated store. File
# deletions (e.g. bridge-full's --delete-fn) are honored — a deleted file is simply
# absent from the copy.
set -euo pipefail

src=${1:?usage: seal_into_volume.sh <source-worktree> <dest-work-volume>}
dst=${2:?usage: seal_into_volume.sh <source-worktree> <dest-work-volume>}

fail() { echo "SEAL FAIL: $*" >&2; exit 1; }

[ -d "$src" ] || fail "source $src does not exist"

# --- copy working-tree bytes into a fresh isolated-store repo --------------
mkdir -p "$dst"
[ -z "$(ls -A "$dst" 2>/dev/null)" ] || fail "dest $dst is not empty"

# Top-level-anchored excludes: .git (no shared store / no proven history), target/
# (Rust build junk; CARGO_TARGET_DIR points elsewhere anyway). Works on GNU + bsdtar.
tar -C "$src" --exclude='./.git' --exclude='./target' -cf - . | tar -x -C "$dst"

# Belt-and-suspenders: a stray .git must NOT have come through (would re-link the
# shared object store and defeat isolation).
[ ! -e "$dst/.git" ] || fail "dest contains a .git after copy — isolation broken"

git -C "$dst" init -q
git -C "$dst" add -A
git -C "$dst" -c user.email=peel@local -c user.name=peel \
    commit -q --no-gpg-sign -m "peeled init state (history sealed)"

sha=$(git -C "$dst" rev-parse HEAD)
git -C "$dst" checkout -q --detach "$sha"

# Drop the init branch ref (master/main) so ONLY detached HEAD reaches the commit.
for ref in $(git -C "$dst" for-each-ref --format='%(refname)' refs/heads/); do
    git -C "$dst" update-ref -d "$ref"
done
# Belt-and-suspenders: no remotes (init makes none, but be explicit).
for r in $(git -C "$dst" remote); do git -C "$dst" remote remove "$r"; done

# --- PREFLIGHT ASSERTIONS (the T112 seal contract) ------------------------
git -C "$dst" rev-parse -q --verify 'HEAD^' >/dev/null 2>&1 \
    && fail "dest HEAD has a parent — seal is not an orphan root"
dst_subject=$(git -C "$dst" log -1 --format=%s)
case "$dst_subject" in
    *"history sealed"*) ;;
    *) fail "dest sentinel missing (_is_sealed_worktree would reject)" ;;
esac
fsck=$(git -C "$dst" fsck --no-reflogs --unreachable --no-progress 2>&1 || true)
[ -z "$fsck" ] || fail "dest fsck not clean (dangling objects present):\n$fsck"
[ -z "$(git -C "$dst" for-each-ref refs/heads refs/remotes refs/tags)" ] \
    || fail "dest has stray refs beyond detached HEAD"

echo "SEAL OK $dst @ $sha"
