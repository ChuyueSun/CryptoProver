# Creating a clean admitted worktree

A run wants the target in its **admitted starting state** — `proof fn`
bodies replaced by `admit()`, with `spec fn` defs, exec code, and
`axiom_*` left intact — inside an isolated checkout so the run never
dirties your main tree. The dalek-lite benchmark repo ships that state pre-built at the
**`eval/admitted-start`** ref; this repo's [`admit.py`](../admit.py) is the
single-file tool that recreates it: `admit.py --worktree` does the checkout,
and its body pass (also `launch.sh --admit`) performs the admission.

The dalek-lite project is a Cargo **workspace**: the worktree is added at
the git **repo root** (`.../dalek-lite`, which holds the workspace
`Cargo.toml`); the `curve25519-dalek/` **member** subdir is what you pass
as `--project`. Two ways to get the worktree, both verified end-to-end:

```bash
REPO=/path/to/dalek-lite                 # project git repo root (the Cargo workspace)

# A — check out the pre-built admitted ref (skeleton already committed):
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref eval/admitted-start
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek           # edwards.rs already has 92 admits

# B — build the skeleton from clean proven source. --detach is implicit, so this
#     works even though the primary checkout already holds `main`; --admit-target
#     admits that file in place after checkout (0 → 92 admits for edwards.rs):
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref main \
    --admit-target curve25519-dalek/src/edwards.rs
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek

# tear the worktree down when done
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --remove
```

`admit.py --worktree` is just `git worktree add --detach <dest> <ref>`
plus the optional in-place body pass, so the by-hand equivalent is
`git -C "$REPO" worktree add --detach /tmp/dalek-wt <ref>` then
`launch.sh --admit` / `python admit.py <file> --in-place`. The body pass
**resets any proofs already present**, so it must run on a clean checkout
— a fresh worktree is exactly that. A reuses the pre-built ref; B reconstructs the admission locally.

On a **brand-new worktree**, warm vstd once before the first run —
`cargo verus verify -p curve25519-dalek` from the member dir (~40s) —
otherwise the first module-scoped `verus_check` spuriously fails
(`--verify-module` leaks into an uncompiled vstd → "could not find
module").

**Running several at once.** Each worktree is a fully isolated checkout,
so you *can* fan out parallel runs — give each its own worktree **and**
its own `--results` dir, then one `launch.sh` per worktree. The worktree
clears cargo's `.cargo-lock` (builds serialize on one project root); the
separate `--results` clears the `failure_memory.json` /
`proven_registry.json` / `catalog_cache.json` read-modify-write race
(those are keyed off the results root, not the project). Sharing either
reintroduces a race:

```bash
git -C "$REPO" worktree add --detach /tmp/dalek-wt-1 eval/admitted-start
./launch.sh --detach --run-id par_edw  --project /tmp/dalek-wt-1/curve25519-dalek \
    --vstd-root "$VSTD" --results results-edw  src/edwards.rs
git -C "$REPO" worktree add --detach /tmp/dalek-wt-2 eval/admitted-start
./launch.sh --detach --run-id par_rist --project /tmp/dalek-wt-2/curve25519-dalek \
    --vstd-root "$VSTD" --results results-rist src/ristretto.rs
# watch:  tail -f launcher_par_*.log | grep --line-buffered '^MARKER'
```
